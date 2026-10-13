# CLAUDE.md

## Project

`Trim_mp4.py` is a single-file Python CLI (standard library only) that trims and cuts mp4 files by calling `ffmpeg` and `ffprobe` through `subprocess`. User-facing documentation is in `README.md`; the in-script help text is the `HELP` constant.

## Running and testing

There is no test suite. Verify changes by generating a clip and running the script on it. Do this in a scratch directory, not in the repo:

```
ffmpeg -f lavfi -i testsrc=duration=30:size=320x240:rate=24 -f lavfi -i sine=duration=30 -c:v libx264 -g 120 -c:a aac -shortest clip.mp4
python /path/to/Trim_mp4.py -F clip.mp4 -B 10.5 -E 20 -D 12 14 --dryrun
```

Check the output duration with `ffprobe`. Accurate mode should match the requested kept length exactly; `-Q` (quick) legitimately differs because it snaps to keyframes.

## Conventions

- **Switches:** every option has a `--long` form plus a single-letter form that is case-insensitive (`-B` and `-b`). New options must follow this. They are registered through the `opt()` helper in `build_parser()`. Do not reuse a letter case-insensitively: `-E` is end, `-X` is extract, `-L` is the segment list, so there is no `-l`-style switch for `--log`.
- **Help:** keep the `HELP` constant, `build_parser()` and `README.md` in sync whenever a switch changes.
- **Time model:** `-B`, `-E`, `-D` and `-L` are time points on the original video. `plan_cuts()` turns them into merged `removed` intervals and `kept` intervals. Everything downstream (extraction, accurate cut, quick cut) works from `kept`. The end point is exclusive.
- **Frame accuracy:** boundaries are shifted a quarter frame early (`snap()` and the extraction seek) so float rounding can never drop or add a frame. Keep that behaviour if you touch cut or extraction code.
- **Output safety:** write to a per-thread temp file (`temp_name()`) and `os.replace()` on success. Never write straight onto the source. Generated names use `unique_name()` so earlier output is never clobbered.
- **Dry run:** every code path that writes must go through `run_cmd(cmd, dry)` or check `args.dryrun`, so `--dryrun` stays side-effect free.
- **Errors:** catch specific exceptions (`OSError`, `VideoError`), log a per-file message, and return `False` so one bad file does not stop a batch. No bare `except:`.
- **Logging:** use the `log` logger, not `print`, so output reaches both the console and the `--log` file. `log.debug` goes to the log file only.
- **Parallelism:** files are processed in threads, so anything shared between files (temp names, work directories) must be unique per thread.

## Platform notes

Developed on Windows with Python 3.14. Paths handed to ffmpeg's `subtitles` filter and concat list files need explicit escaping; see `do_subtitles()` and `cut_quick()`.
