# trim_mp4

A command-line tool for trimming and cutting mp4 files with ffmpeg. It is frame accurate by default, can extract the frames at the cut points so you can verify a cut before committing to it, and can burn in subtitles. It works on single files or whole directory trees, in parallel.

## Requirements

- Python 3.9 or newer
- `ffmpeg` and `ffprobe` on your PATH
- No Python packages beyond the standard library
- Optional: an NVIDIA GPU with NVENC support for `--gpu`

## Quick start

```
python Trim_mp4.py -F movie.mp4 -B 10.5 -E 95
```

This keeps 10.5 s to 95 s of `movie.mp4` and writes `movie_trimmed.mp4` next to it. The original is left untouched.

Run `python Trim_mp4.py -h` for the full switch list.

## Switches

Short switches are case-insensitive (`-b` is the same as `-B`), and every switch also has a `--long` form. Abbreviated long options are not accepted.

### Selecting files

| Switch | Description |
|---|---|
| `-C`, `--current` | All mp4 files in the current directory |
| `-R`, `--recursive` | All mp4 files in the current directory and below |
| `-F`, `--file PATTERN` | A specific file or wildcard pattern, e.g. `"clips/*.mp4"` (`**` works) |

At least one is required. If more than one is given, `-F` wins, then `-R`, then `-C`.

When scanning with `-C` or `-R`, files produced by earlier runs (`*_trimmed.mp4`, `*_subtitled.mp4`) are skipped. A file named explicitly with `-F` is always processed.

### What to keep or remove

All values are time points on the **original** video.

| Switch | Description |
|---|---|
| `-B`, `--begin N` | The kept video starts at N |
| `-E`, `--end N` | The kept video ends at N. The frame at N itself is not included |
| `-D`, `--delete A B` | Remove everything from A up to B. Repeatable |
| `-L`, `--list FILE` | Remove the segments listed in FILE |
| `-T`, `--time s\|f` | Interpret values as seconds (default) or frame numbers |

Values can be decimal: `-B 10.5` starts at 10 seconds and 500 ms, which is frame 252 on a 24 fps video. With `-T f` the values are frame numbers, so `-B 240` starts at frame 240.

`-B`, `-E`, `-D` and `-L` can be combined freely. Overlapping and touching ranges are merged. Values beyond the end of the video are clamped (with a warning where it matters), and a combination that would remove the whole video is rejected.

### Verifying the cut points

| Switch | Description |
|---|---|
| `-X`, `--extract` | Do not trim. Save the frames at the cut points as jpg files instead |

For every cut, two frames are saved: the last frame kept before the cut and the first frame kept after it. With only `-B`, just the first kept frame is saved. With only `-E`, just the last kept frame is saved. Cuts at the very start or end of the video are skipped, since nothing is cut there.

Files are written next to the source and named `<name>_extract_<time>.jpg`, where `<time>` is the time of that frame in seconds (`clip_extract_10.5.jpg`), or `f<frame>` with `-T f` (`clip_extract_f240.jpg`).

Because the end point is exclusive, `-E 20` on a 24 fps video extracts the frame at 19.958 s, which is the last frame actually kept. Extracted frames are named after their own time, not the value you typed.

A typical workflow is to run with `-X` first, look at the images, and then run the same command without `-X`.

### Encoding and output

| Switch | Description |
|---|---|
| `-Q`, `--quick` | Stream copy. Very fast with no quality loss, but cuts snap to keyframes and can be off by seconds |
| `--gpu` | Encode with NVENC (`h264_nvenc`) instead of `libx264`. Falls back to CPU if the GPU encode fails |
| `-J`, `--jobs N` | Process N files in parallel (default 1) |
| `-O`, `--overwrite` | Replace the source file instead of writing `<name>_trimmed.mp4` |
| `-S`, `--subtitle FILE` | Burn in subtitles (see [Subtitles](#subtitles)). Cannot be combined with trimming or extraction |

The default mode re-encodes (video `libx264` CRF 18, audio AAC 192k) so cuts land on exactly the frame you asked for. Only the first video stream and the first audio stream are kept.

Output files are never overwritten by accident: if `clip_trimmed.mp4` exists, the next run writes `clip_trimmed_1.mp4`, and so on.

With `-O`, the result is written to a temporary file and only replaces the source when ffmpeg has finished successfully.

### Testing, logging and help

| Switch | Description |
|---|---|
| `--dryrun` | Show what would be done and the exact ffmpeg commands, without writing anything |
| `--log [FILE]` | Also write a log file. Defaults to `trim_mp4_<date>_<time>.log` in the current directory |
| `-H`, `-h`, `-?`, `--help` | Show help |

`--dryrun` and `--log` can be combined, which is a good way to record a plan before running it. The log file contains timestamps and the full ffmpeg command for every step.

## Subtitles

```
python Trim_mp4.py -F movie.mp4 -S captions.srt
```

`-S`, `--subtitle FILE` burns the subtitles permanently into the picture (hardcoded) using ffmpeg's `subtitles` filter. The result is written as `<name>_subtitled.mp4`; the original is untouched. The filter is provided by libass, so your ffmpeg build must include it. `.srt` is the usual format, and other formats the filter reads, such as `.ass`, should also work.

How it behaves:

- **Exclusive mode.** Subtitles cannot be combined with `-B`, `-E`, `-D`, `-L` or `-X`. The script stops with an error (exit code 2) before touching any file. To trim and add subtitles, do it in two runs: trim first, then run `-S` on the `_trimmed` file. Subtitle timestamps are relative to the start of the video being processed, so the subtitle file must match the trimmed video, not the original.
- **Re-encoding.** The video is re-encoded (`libx264` CRF 18), because burned-in text changes the picture. The audio stream is copied unchanged. `-Q` has no effect here and `-T` is not used.
- **GPU.** `--gpu` encodes with NVENC (`h264_nvenc`) instead of `libx264`, and falls back to the CPU if the GPU encode fails.
- **One subtitle file per run.** In a batch (`-C`, `-R` or a wildcard `-F`) the same subtitle file is applied to every video, so subtitles are normally used with a single file selected via `-F`.
- **Output.** `-O` replaces the source file, written via a temporary file so a failed encode never damages the original. Without it, an existing `_subtitled.mp4` is never overwritten; the next run writes `_subtitled_1.mp4`.
- **Checks before work starts.** A missing or unreadable subtitle file stops the run with exit code 1. A subtitle path containing an apostrophe (`'`) is rejected; rename the file.
- **Other switches.** `-J`, `--dryrun` and `--log` work as usual. `--dryrun` prints the ffmpeg command without writing anything.

## Segment files

`-L` reads one segment per line as `start end`. Blank lines are ignored, and `#` starts a comment.

```
# intro and credits
0 12.5
1800 1865
```

Values use the same unit as the rest of the run, so add `-T f` if the file contains frame numbers.

## Examples

Keep 10.5 s to 95 s:

```
python Trim_mp4.py -F movie.mp4 -B 10.5 -E 95
```

Frame numbers instead of seconds:

```
python Trim_mp4.py -F movie.mp4 -T f -B 240 -E 5000
```

Check where the cuts fall before trimming:

```
python Trim_mp4.py -F movie.mp4 -T f -B 240 -E 5000 -X
```

Remove two segments from every mp4 in the current directory, four files at a time:

```
python Trim_mp4.py -C -D 10 20 -D 30 40 -J 4
```

Preview a recursive batch from a segment file and keep a log:

```
python Trim_mp4.py -R -L cuts.txt --dryrun --log
```

Fast, approximate trim of the first minute:

```
python Trim_mp4.py -F movie.mp4 -E 60 -Q
```

Burn in subtitles on the GPU:

```
python Trim_mp4.py -F movie.mp4 -S captions.srt --gpu
```

## Error handling

- Unreadable sources, write-protected files or folders, files locked by another program, and corrupt or non-mp4 input are reported per file with a clear message. A failure on one file does not stop the rest of a batch.
- Temporary files are cleaned up whether a run succeeds or fails.
- The exit code is `0` if every file succeeded and `1` otherwise. Invalid switch combinations exit with `2`.

## Limitations

- Only mp4 files are processed.
- Quick mode (`-Q`) cuts at keyframes, so the result can differ from what `-X` shows. Use the default mode when exactness matters.
- Subtitles are burned in only; they cannot be added as a selectable track, and cannot be combined with trimming in one run.
- Subtitle paths containing an apostrophe are not supported.
- Frame rate is read from the first video stream. Variable frame rate video is supported, but frame numbers are approximate for it.
