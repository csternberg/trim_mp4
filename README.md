# trim_mp4

Trim or cut mp4 files (frame accurate by default), extract the frames at the cut points for verification, or burn in subtitles.

Requires Python 3.9+ and `ffmpeg`/`ffprobe` on PATH (the `ffmpeg-python` package is no longer needed).

Run `python Trim_mp4.py -h` for all switches. Short switches are case-insensitive; every switch also has a `--long` form.

```
Trim_mp4.py -F movie.mp4 -B 10.5 -E 95          keep 10.5s - 95s
Trim_mp4.py -F movie.mp4 -T f -B 240 -E 5000 -X check the cut frames first (no trimming)
Trim_mp4.py -C -D 10 20 -D 30 40 -J 4           remove segments, 4 files in parallel
Trim_mp4.py -R -L cuts.txt --dryrun --log       test run with a log file
```
