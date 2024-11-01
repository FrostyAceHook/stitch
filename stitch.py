import argparse
import shutil
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path



# Turns the given class into a singleton, instantiating it once.
def singleton(cls):
    instance = cls()
    def throw(*args, **kwargs):
        msg = f"cannot create another instance of singleton '{cls.__name__}'"
        raise TypeError(msg)
    cls.__new__ = throw
    return instance



# Queries the user for a yes or no response.
@singleton
class ask:
    _always_yes = False
    NONE = 0 # not in a context.
    NO = 1 # in a context, without always saying yes.
    YES = 2 # in a context, always saying yes.
    _ctx = NONE

    def __call__(self, query):
        options = "(y/n/a)" if self._ctx else "(y/n)"
        print(f"{query} {options}: ", end="")
        if self._always_yes or self._ctx == self.YES:
            print("y")
            return True
        while True:
            user_input = input().strip().casefold()
            if user_input == "y":
                return True
            if user_input == "n":
                return False
            if self._ctx and user_input == "a":
                self._ctx = self.YES
                return True

    def always_yes(self):
        self._always_yes = True

    def __enter__(self):
        if self._ctx:
            raise Exception("already in 'ask' context")
        self._ctx = self.NO

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._ctx:
            raise Exception("not in 'ask' context")
        self._ctx = self.NONE



class StitchError(Exception):
    pass
def error(msg):
    raise StitchError(msg)



def esc(path):
    s = str(path)
    quote = "'"
    if "'" in s:
        if "\"" in s:
            s = s.replace("\\", "\\\\")
            s = s.replace("\"", "\\\"")
            s = s.replace("'", "\\'")
        else:
            quote = "\""
    return quote + s + quote

def pathlist(paths):
    return ", ".join(map(esc, paths))


def validate_filename(filename, only_valid_windows):
    # https://stackoverflow.com/a/31976060

    DFLT = "file"

    # most control codes are valid on unix but i dont wanna create a file which
    # is insanely difficult to remove/access.
    replace = {chr(i) for i in range(32)}
    replace |= {"/"}
    if only_valid_windows:
        replace |= {"<", ">", ":", "\"", "/", "\\", "|", "?", "*"}
    for char in replace:
        filename = filename.replace(char, "_")

    if only_valid_windows:
        if filename.endswith(" ") or filename.endswith("."):
            filename = filename[:-1] + "_"

    if only_valid_windows:
        illegal_names = {"con", "prn", "aux", "nul"}
        illegal_names |= {f"com{i}" for i in range(1, 10)}
        illegal_names |= {f"lpt{i}" for i in range(1, 10)}
        name = filename.split(".", 1)[0]
        if name.casefold() in illegal_names:
            return DFLT

    # the so answer doesnt say this is invalid on windows but like. come on. how
    # they supposed to create/open this file for stitching.
    if filename in {".", ".."}:
        return DFLT

    return filename


def unique_paths(paths):
    seen = set()
    unique = []
    for path in paths:
        abspath = path.resolve()
        if abspath in seen:
            continue
        seen.add(abspath)
        unique.append(path)
    return unique


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


def ensure_empty(path):
    if path.is_file():
        if not ask(f"file exists at {esc(path)}, overwrite?"):
            return False
        delete_paths(path)
    elif path.is_dir():
        if not ask(f"directory exists at {esc(path)}, overwrite?"):
            return False
        delete_paths(path)
    return True


def create_empty_dir(path):
    # if it already exists and is empty, great success.
    if path.is_dir() and not any(path.iterdir()):
        return
    if not ensure_empty(path):
        error(f"cannot create directory at: {esc(path)}")
    path.mkdir()


def open_for_write(path):
    if not ensure_empty(path):
        error(f"cannot create file at: {esc(path)}")
    return path.open("wb")


class NoExiste:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        return False
NO_EXISTE = NoExiste()

def open_for_read(path, ignorable=True, askable=True):
    fine = True
    ignore = ignorable
    if not path.exists():
        fine = False
        if ignorable and askable:
            ignore = ask(f"file {esc(path)} doesn't exist, ignore?")
    elif not path.is_file():
        fine = False
        if ignorable and askable:
            ignore = ask(f"path {esc(path)} is not a file, ignore?")
    if not fine:
        if ignore:
            return NO_EXISTE
        error(f"file doesn't exist at: {esc(path)}")
    return path.open("rb")




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




def split_file(path, size, nest, there, compress, delete_original,
        only_valid_windows, validate_filenames):
    path = Path(path)

    # save the original filename to store in the sections.
    filename = path.name
    # dont include spaces for output file paths.
    stem = path.stem.replace(" ", "_")

    if validate_filenames:
        filename = validate_filename(filename, only_valid_windows)
        stem = validate_filename(stem, only_valid_windows)

    makepath = path.with_name if (there) else Path
    nameof = lambda i: makepath(f"{stem}_{i}{EXT}")

    if nest:
        # Create a directory to hold them all.
        dirpath = makepath(f"{stem}_sections")
        create_empty_dir(dirpath)
        nameof = lambda i: dirpath / f"{stem}_{i}{EXT}"
    else:
        # Otherwise, check that no sections files already exist for this file.
        def matching_section_file(p):
            if not p.is_file():
                return False
            if p.suffix != EXT:
                return False
            try:
                with p.open("rb") as file:
                    buf = file.read(Header.SIZE)
                header = Header.read(buf)
                return header.name == filename
            except Exception:
                return False
        parent = path.parent if (there) else Path(".")
        matching = [p for p in parent.iterdir() if matching_section_file(p)]
        if matching:
            if not ask(f"section files already exist for {esc(path)}, "
                    "overwrite?"):
                error(f"section files already exist for: {esc(path)}, at: "
                        + pathlist(matching))
            delete_paths(*matching)

    index = 0
    section_paths = []

    def process(chunk, last):
        nonlocal index

        this = nameof(index)
        header = Header(name=filename, index=index, comp=compress, last=last)
        with open_for_write(this) as file:
            try:
                # print the next one now, but after a possible query of replace.
                if not last:
                    print(f"  {esc(nameof(index + 1))}")
                file.write(header.write())
                file.write(chunk)
            finally:
                # track it to delete if a write fails/keyboard interrupt.
                section_paths.append(this)
        index += 1

    print(f"Splitting {esc(path)} into:")
    try:
        with open_for_read(path) as file:
            if file is not NO_EXISTE:
                # dodgy print re-order to not stall while writing.
                print(f"  {esc(nameof(0))}")
                with ask: # context for overwriting existing files.
                    chunkify(file, compress, size - Header.SIZE, process)
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

    # unpack directories into the subpaths.
    allpaths = []
    dirpaths = []

    def unpack(path):
        if not path.exists():
            if not ask(f"path {esc(path)} doesn't exist, ignore?"):
                error(f"path doesn't exist at: {esc(path)}")
            return

        if not path.is_dir():
            allpaths.append(path)
            return

        subpaths = [p for p in path.iterdir() if p.is_file() and p.suffix == EXT]
        if implicit:
            allpaths.extend(subpaths)
            return
        if subpaths:
            allpaths.extend(subpaths)
            dirpaths.append(path)
            return
        if not ask(f"directory {esc(path)} contains no section files, ignore?"):
            error(f"no sections in directory: {esc(path)}")

    with ask: # context for skipping bad paths.
        for path in paths:
            unpack(path)


    # ensure uniqueness.
    paths = unique_paths(allpaths)


    # Collate all the stitches and their section files.
    @dataclass
    class Stitch:
        sections: dict # index -> path
        count: int
        comp: bool
    # filename -> Stitch
    stitches = {}

    def register(file):
        if file is NO_EXISTE:
            return
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

            # push the error for multi-last to later. assume the earlier last is
            # correct.
            st = stitches[filename]
            if header.last:
                if not st.count:
                    st.count = header.index + 1
                else:
                    st.count = min(st.count, header.index + 1)
        except Exception as e:
            if not ask(f"bad section file {esc(path)}, ignore?"):
                error(f"{str(e)}: {esc(path)}")

    def check(filename, stitch):
        # Check for extra files, i.e. files after last.
        extra = [path for index, path in stitch.sections.items()
                if index >= stitch.count]

        # Check for missing files, i.e. gaps or never reaches last.
        missing = not stitch.count or any(index not in stitch.sections.keys()
                for index in range(stitch.count))

        if missing:
            if not ask(f"missing section files for {esc(filename)}, ignore?"):
                error(f"missing sections for: {esc(filename)}, have sections: "
                        + pathlist(stitch.sections.values()))
            return False # we cannot complete this stitch.
        if extra:
            if not ask(f"unneeded section files for {esc(filename)}, ignore?"):
                error(f"unneeded sections for: {esc(filename)}, bad sections: "
                        + pathlist(extra))
            stitch.sections = {index: path
                    for index, path in stitch.sections.items()
                    if index < stitch.count}
            # stitch is still completeable.

        return True

    with ask: # context to ignore bad sections.
        # Register all sections.
        for path in paths:
            with open_for_read(path) as file:
                register(file)

        # Check all the sections are there.
        to_delete = []
        for filename, stitch in stitches.items():
            if not check(filename, stitch):
                to_delete.append(filename)
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
                # delete the half-baked file before exiting.
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




class StitchHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        if usage is argparse.SUPPRESS:
            return
        # dont include actions which are only active in some modes.
        universal = lambda a: not a.container.title.startswith("when ")
        actions = filter(universal, actions)
        args = usage, actions, groups, prefix
        self._add_item(self._format_usage, args)

class StitchArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        # Allow for many different help requests.
        if any(c in args for c in {"?", "-?", "/?", "/h", "/help"}):
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
                raise argparse.ArgumentTypeError(f"size cannot be <= 0: {size}")
            return size

    raise argparse.ArgumentTypeError(f"invalid size: {arg}, requires: "
            "<num> [k|m|g] B")


def main():
    parser = StitchArgumentParser(prog="stitch",
            description="Stitch/split files from/to smaller files. If no "
                "arguments are given, stitches all the section files in the "
                "current directory.",
            formatter_class=StitchHelpFormatter)

    parser.add_argument("files", type=str, nargs="*", metavar="PATH",
            help="path(s) for file(s) to stitch/split. These are treated as "
                "globs unless '--no-glob' is given."
                " When stitching, directories will be searched for section "
                    "files."
                " When splitting, directories will not be globbed.")

    parser.add_argument("-y", "--yes", action="store_true",
            help="automatically say yes to prompts")

    parser.add_argument("-s", "--split", action="store_true",
            help="split (instead of stitch) each of the given file(s)")

    parser.add_argument("--no-glob", action="store_true",
            help="do not treat paths as globs")

    parser.add_argument("--empty-glob", action="store_true",
            help="silently ignore empty globs")


    group_stitch = parser.add_argument_group("when stitching")

    group_stitch.add_argument("-k", "--keep-sections", action="store_true",
            help="keep section files after stitching")

    group_stitch.add_argument("--keep-dirs", action="store_true",
            help="keep empty directories after stitching")


    group_split = parser.add_argument_group("when splitting")

    group_split.add_argument("-x", "--size", type=parse_size, metavar="SIZE",
            help="maximum size of the sections (defaults to 8MB)")

    group_split.add_argument("-f", "--fast", action="store_true",
            help="faster split (does no compression)")

    group_split.add_argument("-r", "--replace", action="store_true",
            help="delete original file after splitting")

    group_split.add_argument("-n", "--nest", action="store_true",
            help="output sections into separate directories")

    group_split.add_argument("-t", "--there", action="store_true",
            help="output sections relative to the original file")

    group_split.add_argument("--unix-filenames", action="store_true",
            help="allow filenames that may be invalid on windows")

    group_split.add_argument("--all-filenames", action="store_true",
            help="disable filename validation")


    args = parser.parse_args()


    # ensure that splitting/sitching arguments are only given if the mode
    # actually matches. typically this would be done by argparse with a subparser
    # but i didnt wanna use them soo.
    illegal_group = group_stitch if args.split else group_split
    # cheeky access of all options.
    illegals = [x.option_strings for x in parser._actions
            if x.container is illegal_group and getattr(args, x.dest)]
    if illegals:
        illegals = ["/".join(f"'{y}'" for y in x) for x in illegals]
        if len(illegals) <= 2:
            illegal = " or ".join(illegals)
        else:
            illegals[-1] = "or " + illegals[-1]
            illegal = ", ".join(illegals)
        parser.error(f"cannot specify {illegal} {illegal_group.title}")


    if not args.files and args.split:
        parser.error("specify at-least one file to split")
    # no files is fine for stitching (handled in `stitch_files`).

    if args.yes:
        ask.always_yes()

    if not args.size:
        args.size = 8 << 20 # 8MB

    if args.size <= Header.SIZE:
        error(f"cannot encode any data without at-least {Header.SIZE + 1} byte "
                "sections")


    # Handle globbing.
    if not args.no_glob:
        paths = []
        for path in args.files:
            matching = list(Path(".").glob(path))
            # if splitting, only match files.
            if args.split:
                matching = [p for p in matching if p.is_file()]
            if not matching:
                if args.empty_glob:
                    continue
                if not ask(f"glob {esc(path)} matches nothing, ignore?"):
                    error(f"glob matched nothing: {esc(path)}")
            paths.extend(matching)
    else:
        paths = args.files

    # remove duplicate paths. note this is mostly done to ensure consistency
    # between glob and no glob.
    paths = unique_paths(paths)


    # Do the thing.
    if args.split:
        for path in paths:
            split_file(path, size=args.size, nest=args.nest, there=args.there,
                    compress=not args.fast, delete_original=args.replace,
                    only_valid_windows=not args.unix_filenames,
                    validate_filenames=not args.all_filenames)
    else:
        stitch_files(paths, keep_sections=args.keep_sections,
                keep_dirs=args.keep_dirs)


if __name__ == "__main__":
    try:
        main()
    except StitchError as e:
        sys.stderr.write(f"stitch: error: {e}\n")
        sys.exit(1)
