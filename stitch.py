import argparse
import os
import shutil
import struct
import zlib
from dataclasses import dataclass


QUIET = False



# Section file:
#   .brs extension
EXT = ".brs"

# Header:
#   120 byte string of file name (including og extension).
#   4 byte section index
#   4 byte section total count (initialised to 0, back-updated to total count).
# 128B total.
@dataclass
class Header:
    SIZE = 128
    name: str
    index: int
    count: int
    def __post_init__(self):
        if not (0 <= self.index < 2**32):
            raise ValueError("index must be a 4B unsigned integer")
        if not (0 <= self.count < 2**32):
            raise ValueError("count must be a 4B unsigned integer")
        if self.count > 0 and self.index >= self.count:
            raise ValueError("invalid index")

    @classmethod
    def read(cls, buf):
        if len(buf) != cls.SIZE:
            raise ValueError("header must be 128B")

        name = buf[:cls.SIZE - 8].rstrip(b"\x00").decode("utf-8")
        index, = struct.unpack_from("<I", buf, cls.SIZE - 8)
        count, = struct.unpack_from("<I", buf, cls.SIZE - 4)

        return cls(name=name, index=index, count=count)

    def write(self):
        name_size = self.SIZE - 8
        name = self.name.encode("utf-8")[:name_size].ljust(name_size, b"\x00")
        index = struct.pack("<I", self.index)
        count = struct.pack("<I", self.count)

        return name + index + count

    @classmethod
    def update_count(cls, path, count):
        with open(path, "r+b") as file:
            file.seek(cls.SIZE - 4)
            file.write(struct.pack("<I", count))



def esc(s, /):
    # return '"' + s.replace('"', '""') + '"'
    return "'" + s.replace("'", "''") + "'"

def ask(query):
    if getattr(ask, "always_yes", False):
        return True
    while True:
        user_input = input(f"{query} (y/n/a): ")
        if not user_input:
            continue
        if user_input.lower() == "y":
            return True
        if user_input.lower() == "a":
            setattr(ask, "always_yes", True)
            return True
        print("Canceled.")
        return False


def open_for_write(path):
    if os.path.exists(path):
        if not ask(f"file {esc(path)} already exists, overwrite?"):
            raise Exception(f"file already exists at: {esc(path)}")
    return open(path, "wb")

class NoExiste:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        return False
NO_EXISTE = NoExiste()

def open_for_read(path, ignorable=True):
    if ignorable and not os.path.exists(path):
        if not ask(f"file {esc(path)} doesn't exist, ignore?"):
            raise Exception(f"file doesn't exist at: {esc(path)}")
        return NO_EXISTE
    return open(path, "rb")


def create_dir(path):
    if os.path.exists(path):
        if not ask(f"directory {esc(path)} already exists, overwrite?"):
            raise Exception(f"directory already exists at: {esc(path)}")
        shutil.rmtree(path)
    os.makedirs(path)


def delete_paths(paths):
    exceptions = []
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            exceptions.append((path, str(e)))
    if exceptions:
        raise Exception(f"Failed to delete paths: {exceptions}")




# All read/write operations are this size.
CHUNK = 64 << 10 # 64kB


def chunkify(file, max_size, process):
    compressor = zlib.compressobj()
    data = b""

    # Read/compress the entire file in chunks.
    chunk = file.read(CHUNK)
    while chunk:
        data += compressor.compress(chunk)

        # While we'd still have data left after processing, do it.
        while len(data) > max_size:
            process(data[:max_size], False)
            data = data[max_size:]

        chunk = file.read(CHUNK)

    # Ensure the compressor is flushed.
    data += compressor.flush()

    # While we'd still have data left after processing, do it.
    while len(data) > max_size:
        process(data[:max_size], False)
        data = data[max_size:]

    # Now do the final section.
    process(data[:max_size], True)


def dechunkify(file, get_name):
    decompressor = zlib.decompressobj()

    while (name := get_name()) is not None:
        with open_for_read(name, ignorable=False) as f:
            # Ignore header.
            f.seek(Header.SIZE)

            chunk = f.read(CHUNK)
            while chunk:
                file.write(decompressor.decompress(chunk))
                chunk = f.read(CHUNK)

    file.write(decompressor.flush())




def split_file(path, section_size, nest, delete_original):
    filename = os.path.basename(path)
    name, ext = os.path.splitext(filename)

    nameof = lambda i: f"{name.replace(' ', '_')}_{i}{EXT}"

    if nest:
        # Create a directory to hold them all.
        dirname = f"{name.replace(' ', '_')}_sections"
        create_dir(dirname)
        nnameof = nameof
        nameof = lambda i: f"{dirname}/{nnameof(i)}"

    header = Header(name=filename, index=0, count=0)
    section_paths = []

    def process(chunk, isfinal):
        this = nameof(header.index)
        print(f"  {esc(this)}")
        with open_for_write(this) as file:
            file.write(header.write())
            file.write(chunk)

        section_paths.append(this)
        header.index += 1

        # Update all the counts.
        if isfinal:
            count = header.index
            for path in section_paths:
                Header.update_count(path, count)

    print(f"Splitting {esc(path)} into:")
    try:
        with open_for_read(path) as file:
            if file is not NO_EXISTE:
                chunkify(file, section_size - Header.SIZE, process)
    except:
        if nest:
            delete_paths(dirname)
        else:
            delete_paths(section_paths)
        raise

    if delete_original:
        os.remove(path)


def stitch_files(paths, keep_sections):
    allpaths = []
    for path in paths:
        if os.path.isdir(path):
            for p in os.listdir(path):
                p = os.path.join(path, p)
                if os.path.isfile(p):
                    allpaths.append(p)
        else:
            allpaths.append(path)

    allpaths = [p for p in allpaths if p.endswith(EXT)]

    # First create a mapping of original filename to a dict of the paths which
    # hold each index.
    # filename -> (section_count, section_names [index -> path])
    stitching = {}

    for path in allpaths:
        with open_for_read(path) as file:
            if file is NO_EXISTE:
                continue
            buf = file.read(Header.SIZE)
            try:
                header = Header.read(buf)
                if header.count == 0:
                    raise Exception("unfinished section file")

                if header.name in stitching:
                    count, names = stitching[header.name]
                    if header.count != count:
                        raise Exception("inconsistent section count")
                    if header.index in names:
                        raise Exception("duplicate section file")
                    names[header.index] = path
                else:
                    obj = (header.count, {header.index: path})
                    stitching[header.name] = obj
            except Exception:
                if not ask(f"invalid section file {esc(path)}, ignore?"):
                    raise


    # Check all the sections are there.
    invalid = set()
    for filename, (count, names) in stitching.items():
        if len(names) != count:
            if not ask(f"missing section files for {esc(filename)}, ignore?"):
                raise Exception(f"missing sections for: {esc(filename)}, have "
                        "sections: "
                        f"[{', '.join(esc(x) for x in names.values())}]")
            invalid.add(filename)

    for f in invalid:
        del stitching[f]


    for filename, (count, names) in stitching.items():
        print(f"Stitching {esc(filename)} from:")
        i = 0
        def get_name():
            nonlocal i
            if i == count:
                return None
            name = names[i]
            i += 1
            print(f"  {esc(name)}")
            return name

        with open_for_write(filename) as file:
            try:
                dechunkify(file, get_name)
            except:
                file.close()
                os.remove(filename)
                raise

        if not keep_sections:
            delete_paths(names.values())







def parse_size(arg):
    arg = arg.lower()
    units = {"b": 1, "kb": 1 << 10, "mb": 1 << 20, "gb": 1 << 30}
    for unit in sorted(units, key=len, reverse=True):
        if arg.endswith(unit):
            size = float(arg[:-len(unit)])
            return int(size * units[unit])

    raise argparse.ArgumentTypeError(f"Invalid size format: {arg}")


def main():
    global QUIET

    parser = argparse.ArgumentParser(prog="brstitch",
            description="Stitch/split files from/to smaller files. If no "
                    "arguments are given, stitches all the section files in the "
                    "current directory. Section files (which can be stitched "
                    f"back together) must have the extension '{EXT}'.")

    parser.add_argument("files", type=str, nargs="*", metavar="PATH",
            help="path(s) for file(s) to stitch/split")

    parser.add_argument("-y", "--yes", action="store_true",
            help="always say yes")

    parser.add_argument("-q", "--quiet", action="store_true",
            help="don't print to console")

    parser.add_argument("-s", "--split", action="store_true",
            help="split each of the given file(s) into sections")

    parser.add_argument("-k", "--keep-sections", action="store_true",
            help="[when stitching] keep the section files after stitching")

    parser.add_argument("-r", "--replace", action="store_true",
            help="[when splitting] delete the original file after splitting")

    parser.add_argument("-n", "--nest", action="store_true",
            help="[when splitting] place the sections of each file into a "
                    "separate directory")

    parser.add_argument("-x", "--size", type=parse_size, metavar="SIZE",
            help="[when splitting] maximum size of the sections (defaults to "
                    "8MB)")


    args = parser.parse_args()

    QUIET = args.quiet

    if not args.files and not args.split:
        args.files = ["."]

    if args.yes:
        setattr(ask, "always_yes", True)

    if args.size is None:
        args.size = 8 << 20 # 8MB

    if args.size <= Header.SIZE:
        raise Exception("cannot encode any data without at-least "
                f"{Header.SIZE + 1} byte sections")

    # Do the thing.
    if args.split:
        for path in args.files:
            split_file(path, section_size=args.size, nest=args.nest,
                    delete_original=args.replace)
    else:
        stitch_files(args.files, keep_sections=args.keep_sections)


if __name__ == "__main__":
    main()
