# stitch

Simple command-line utility to split files into smaller files and then stitch
them back up.

``` py stitch.py -h ```

[bin/](bin/) contains two batch files, `stitch` and `split`, which are shorthands
for `py stitch.py` and `py stitch.py -s`.

[aptly named folder](addtowindowsmenu/) contains two Windows registry files to
add or remove right-click menu shortcuts for File Explorer. These require:
- `py.exe` (the python executor) to be within the Windows folder.
- The [stitch script](stitch.py) to be within `C:\Program Files\br_stitch\`
    (or you can modify the registry file to point wherever it's installed).

The shortcuts themselves are an option to split a file when right-clicking it,
and an option to stitch all files in the current folder when right-clicking the
background. Note that a command prompt will flicker open, and may stay open if
any queries need to be answered (i.e. can overwrite file?).
