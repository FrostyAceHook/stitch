import argparse
import shutil
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path



def ask(query):
    if getattr(ask, "always_yes", False):
        return True
    while True:
        user_input = input(f"{query} (y/n/a): ")
        user_input = user_input.strip().casefold()
        if user_input == "y":
            return True
        if user_input == "n":
            return False
        if user_input == "a":
            setattr(ask, "always_yes", True)
            return True



def error(msg):
    sys.stderr.write(f"stitch: error: {msg}\n")
    sys.exit(3)



def esc(s, /):
    return "'" + str(s).replace("'", "''") + "'"



def delete_paths(*paths):
    exceptions = []
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except Exception as e:
            exceptions.append((path, str(e)))
    if exceptions:
        ex = "".join(f"\n  {p} -> {e}" for p, e in exceptions)
        error(f"failed to delete paths:{ex}")


def open_for_write(path):
    if path.exists():
        if not ask(f"file {esc(path)} already exists, overwrite?"):
            error(f"file already exists at: {esc(path)}")
        delete_paths(path)
    return path.open("wb")


class NoExiste:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        return False
NO_EXISTE = NoExiste()

def open_for_read(path, ignorable=True, askable=True):
    fine = True
    ignore = False
    if not path.exists():
        fine = False
        if ignorable:
            if askable:
                ignore = ask(f"file {esc(path)} doesn't exist, ignore?")
            else:
                ignore = True
    elif not path.is_file():
        fine = False
        if ignorable:
            if askable:
                ignore = ask(f"path {esc(path)} is not a file, ignore?")
            else:
                ignore = True
    if not fine:
        if ignore:
            return NO_EXISTE
        error(f"file doesn't exist at: {esc(path)}")
    return path.open("rb")


def create_empty_dir(path):
    # if it already exists and is empty, great success.
    if path.is_dir() and not any(path.iterdir()):
        return
    if path.exists():
        if not ask(f"directory {esc(path)} already exists, overwrite?"):
            error(f"directory already exists at: {esc(path)}")
        shutil.rmtree(path)
    path.mkdir()




# Section file uses this extension
EXT = ".brs"

# Section file header:
#   offset  length  desc
#   0       3       magic "BRS"
#   3       1       flags ([0] = is last, [1] = compressed)
#   4       4       u32 section index
#   8       120     string of original file name (including og extension).
# 128 byte total.
@dataclass
class Header:
    SIZE = 128
    MAGIC = b"BRS"
    FLAG_LAST = 0x01
    FLAG_COMP = 0x02

    name: str
    index: int
    comp: bool
    last: bool
    def __post_init__(self):
        if not (0 <= self.index < 2**32):
            raise ValueError("index must be a 4B unsigned integer")

    @classmethod
    def read(cls, buf):
        if len(buf) != cls.SIZE:
            raise ValueError("header must be 128B")

        if buf[:3] != cls.MAGIC:
            raise ValueError("incorrect magic number")

        flags = buf[3]
        if flags & ~(cls.FLAG_LAST | cls.FLAG_COMP):
            raise ValueError("invalid flags")

        index, = struct.unpack_from("<I", buf, 4)
        name = buf[8:].rstrip(b"\x00").decode("utf-8")

        comp = (flags & cls.FLAG_COMP) > 0
        last = (flags & cls.FLAG_LAST) > 0
        return cls(name=name, index=index, comp=comp, last=last)

    def write(self):
        flags = 0
        if self.comp:
            flags |= self.FLAG_COMP
        if self.last:
            flags |= self.FLAG_LAST
        flags = bytes([flags])

        index = struct.pack("<I", self.index)
        NAMESZ = self.SIZE - 8
        name = self.name.encode("utf-8")[:NAMESZ].ljust(NAMESZ, b"\x00")

        return self.MAGIC + flags + index + name




# All read/write operations are this size. Note this is entirely independant from
# section file sizing, this is just so that we can stitch files which we may not
# be able to hold in ram all at once.
CHUNK = 50 << 20 # 50mB


def chunkify(file, compress, max_size, process):
    if compress:
        compressor = zlib.compressobj()
    data = b""

    # Read/compress the entire file in chunks.
    chunk = file.read(CHUNK)
    while chunk:
        if compress:
            data += compressor.compress(chunk)
        else:
            data += chunk

        # While we'd still have data left after processing, do it.
        while len(data) > max_size:
            process(data[:max_size], False)
            data = data[max_size:]

        chunk = file.read(CHUNK)

    # Ensure the compressor is flushed.
    if compress:
        data += compressor.flush()

    # While we'd still have data left after processing, do it.
    while len(data) > max_size:
        process(data[:max_size], False)
        data = data[max_size:]

    # Now do the final section.
    process(data[:max_size], True)


def dechunkify(file, decompress, get_path):
    if decompress:
        decompressor = zlib.decompressobj()

    while (path := get_path()) is not None:
        with open_for_read(path, ignorable=False) as f:
            # Ignore header.
            f.seek(Header.SIZE)

            chunk = f.read(CHUNK)
            while chunk:
                if decompress:
                    data = decompressor.decompress(chunk)
                else:
                    data = chunk
                file.write(data)
                chunk = f.read(CHUNK)

    if decompress:
        file.write(decompressor.flush())




def split_file(path, section_size, nest, compress, delete_original):
    path = Path(path)

    filename = path.name
    # dont let dir separators slip in.
    filename = filename.replace("/", "")
    filename = filename.replace("\\", "")
    filename = filename.replace(":", "") # or this funky guy.
    if not filename:
        filename = "file"

    stem = path.stem.replace(" ", "_")
    nameof = lambda i: path.with_name(f"{stem}_{i}{EXT}")

    if nest:
        # Create a directory to hold them all.
        dirpath = Path(f"{stem}_sections")
        create_empty_dir(dirpath)
        nnameof = nameof
        nameof = lambda i: dirpath / nnameof(i)
    else:
        # Otherwise, check that no sections files already exist for this file.
        matching = []
        for p in Path(".").iterdir():
            try:
                if p.suffix == EXT:
                    with open_for_read(p, askable=False) as file:
                        if file is NO_EXISTE:
                            continue
                        buf = file.read(Header.SIZE)
                        header = Header.read(buf)
                        if header.name == filename:
                            matching.append(p)
            except Exception:
                pass
        if matching:
            if not ask(f"section files already exist for {esc(path)}, "
                    "overwrite?"):
                error(f"section files already exist for: {esc(path)}")
            delete_paths(*matching)

    index = 0
    section_paths = []

    def process(chunk, last):
        nonlocal index

        # print the next one now lmao.
        if not last:
            print(f"  {esc(nameof(index + 1))}")

        this = nameof(index)
        header = Header(name=filename, index=index, comp=compress, last=last)
        try:
            with open_for_write(this) as file:
                file.write(header.write())
                file.write(chunk)
        finally:
            section_paths.append(this)
        index += 1

    print(f"Splitting {esc(path)} into:")
    try:
        with open_for_read(path) as file:
            if file is not NO_EXISTE:
                # dodgy print re-order to not stall while writing.
                print(f"  {esc(nameof(0))}")
                chunkify(file, compress, section_size - Header.SIZE, process)
    except: # mostly for keyboard interrupt
        if nest:
            delete_paths(dirname)
        else:
            delete_paths(*section_paths)
        raise

    if delete_original:
        delete_paths(path)


def stitch_files(paths, keep_sections, keep_dirs):
    if not paths:
        paths = [Path(".")]
        implicit = True
    else:
        paths = [Path(x) for x in paths]
        implicit = False

    allpaths = []
    dirpaths = []
    for path in paths:
        if not path.is_dir():
            allpaths.append(path)

        subpaths = []
        for p in path.iterdir():
            if p.is_file() and p.suffix == EXT:
                subpaths.append(p)
        if implicit:
            allpaths.extend(subpaths)
            continue
        # warn about empty directories (except for an implicit '.')
        if not subpaths:
            if not ask(f"directory {esc(path)} contains no section files, "
                    "ignore?"):
                error(f"no sections in directory: {esc(path)}")
        else:
            allpaths.extend(subpaths)
            dirpaths.append(path)

    # ensure uniqueness.
    unique_paths = set()
    paths = []
    for path in allpaths:
        abspath = path.resolve()
        if abspath in unique_paths:
            continue
        unique_paths.add(abspath)
        paths.append(path)


    # First create a mapping of original filename to a dict of the paths which
    # hold each index.
    # filename -> Stitch
    @dataclass
    class Stitch:
        sections: dict # index -> path
        count: int
        comp: bool
    stitches = {}

    for path in paths:
        with open_for_read(path) as file:
            if file is NO_EXISTE:
                continue
            buf = file.read(Header.SIZE)
            try:
                header = Header.read(buf)
                filename = Path(header.name)

                if filename in stitches:
                    st = stitches[filename]
                    if header.comp != st.comp:
                        raise Exception("inconsistent section compression")
                    if header.index in st.sections:
                        raise Exception("duplicate section file")
                    st.sections[header.index] = path
                else:
                    stitches[filename] = Stitch(
                            sections={header.index: path},
                            count=0,
                            comp=header.comp)

                # push the error for multi-last to later. assume the earlier last
                # is correct.
                st = stitches[filename]
                if header.last:
                    if not st.count:
                        st.count = header.index + 1
                    else:
                        st.count = min(st.count, header.index + 1)

            except Exception as e:
                if not ask(f"bad section file {esc(path)}, ignore?"):
                    error(f"{str(e)}: {esc(path)}")


    # Check all the sections are there.
    to_delete = set()
    for filename, st in stitches.items():
        # Check for extra files, i.e. files after last.
        extra = []
        if st.count > 0: # fallthrough to missing check.
            for index, path in st.sections.items():
                if index >= st.count:
                    extra.append(path)
        if extra:
            if not ask(f"unneeded section files for {esc(filename)}, ignore?"):
                error(f"unneeded sections for: {esc(filename)}, bad sections: "
                        f"{', '.join(esc(x) for x in extra)}")
            st.sections = {index: path for index, path in st.sections.items()
                    if index < st.count}

        # Check for missing files, i.e. gaps or never reaches last.
        missing = False
        if st.count == 0:
            missing = True
        for index in range(st.count):
            if index not in st.sections:
                missing = True
                break
        if missing:
            if not ask(f"missing section files for {esc(filename)}, ignore?"):
                error(f"missing sections for: {esc(filename)}, have sections: "
                        f"{', '.join(esc(x) for x in st.sections.values())}")
            to_delete.add(filename)

    for f in to_delete:
        del stitches[f]



    if not stitches:
        print("Nothing to stitch.")

    for filename, st in stitches.items():
        print(f"Stitching {esc(filename)} from:")
        index = 0
        def get_path():
            nonlocal index
            if index == st.count:
                return None
            path = st.sections[index]
            index += 1
            print(f"  {esc(path)}")
            return path

        with open_for_write(filename) as file:
            try:
                dechunkify(file, st.comp, get_path)
            except:
                file.close()
                delete_paths(filename)
                raise

        if not keep_sections:
            delete_paths(*st.sections.values())

    # bit hacked in but if the stitching cleared out any directories remove them.
    if not keep_sections and not keep_dirs:
        for path in dirpaths:
            if not any(path.iterdir()):
                path.rmdir()




class StitchArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        # Allow for many different help requests.
        if args and any(c in args for c in {"?", "-?", "/?", "/h", "/help"}):
            args = ["-h"]
        return super().parse_args(args, namespace)


def parse_size(arg):
    arg = arg.lower()
    units = {"b": 1, "kb": 1 << 10, "mb": 1 << 20, "gb": 1 << 30}
    for unit in sorted(units, key=len, reverse=True):
        if arg.endswith(unit):
            size = float(arg[:-len(unit)])
            size *= units[unit]
            size = int(size)
            if size <= 0:
                raise argparse.ArgumentTypeError(f"incorrect size: {arg}, size "
                        "cannot be <= 0")
            return size

    raise argparse.ArgumentTypeError(f"incorrect size: {arg}, requires: "
            "<num> [k|m|g] B")


def main():
    parser = StitchArgumentParser(prog="stitch",
            description="Stitch/split files from/to smaller files. If no "
                "arguments are given, stitches all the section files in the "
                "current directory. For section files (which can be stitched "
                "back together) to be automatically recognised, they must have "
                f"the extension '{EXT}'.")

    parser.add_argument("files", type=str, nargs="*", metavar="PATH",
            help="path(s) for file(s) to stitch/split")

    parser.add_argument("-y", "--yes", action="store_true",
            help="automatically say yes to prompts")

    parser.add_argument("-s", "--split", action="store_true",
            help="split (instead of stitch) each of the given file(s)")


    group_stitch = parser.add_argument_group("when stitching")

    group_stitch.add_argument("-k", "--keep-sections", action="store_true",
            help="keep section files after stitching")

    group_stitch.add_argument("--keep-dirs", action="store_true",
            help="keep empty directories after stitching")


    group_split = parser.add_argument_group("when splitting")

    group_split.add_argument("-f", "--fast", action="store_true",
            help="fast split (does no compression)")

    group_split.add_argument("-r", "--replace", action="store_true",
            help="delete original file after splitting")

    group_split.add_argument("-n", "--nest", action="store_true",
            help="output sections into separate directories")

    group_split.add_argument("-x", "--size", type=parse_size, metavar="SIZE",
            help="maximum size of the sections (defaults to 8MB)")


    args = parser.parse_args()

    illegal_group = group_stitch if args.split else group_split
    illegals = []
    for x in parser._actions:
        if x.container is not illegal_group:
            continue
        if getattr(args, x.dest, False):
            illegals.append(x.option_strings[-1])
    if illegals:
        illegals = [f"'{x}'" for x in illegals]
        if len(illegals) <= 2:
            illegal = " or ".join(illegals)
        else:
            illegals[-1] = "or " + illegals[-1]
            illegal = ", ".join(illegals)
        parser.error(f"cannot specify {illegal} when "
                f"{'splitting' if args.split else 'stitching'}")


    if not args.files:
        if args.split:
            parser.error("specify at-least one file to split")

    if args.yes:
        setattr(ask, "always_yes", True)

    if not args.size:
        args.size = 8 << 20 # 8MB

    if args.size <= Header.SIZE:
        error(f"cannot encode any data without at-least {Header.SIZE + 1} byte "
                "sections")

    # Do the thing.
    if args.split:
        for path in args.files:
            split_file(path, section_size=args.size, nest=args.nest,
                    compress=not args.fast, delete_original=args.replace)
    else:
        stitch_files(args.files, keep_sections=args.keep_sections,
                keep_dirs=args.keep_dirs)


if __name__ == "__main__":
    main()
