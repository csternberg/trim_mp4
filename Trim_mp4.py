#!/usr/bin/env python3
"""Trim_mp4 version 6.0

Trim / cut mp4 files (frame accurate by default), extract the frames at the
cut points for verification, or burn subtitles.  Requires ffmpeg and ffprobe
on PATH.  Run with -h for usage.
"""

import argparse
import datetime
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

VERSION = "6.0"
AUTO_LOG = "<auto>"
EPS = 1e-6

log = logging.getLogger("trim_mp4")

VideoInfo = namedtuple("VideoInfo", "duration fps has_audio")

# Output files made by this script; skipped when scanning with -C / -R
GENERATED = re.compile(r"_(trimmed|subtitled)(_\d+)?$", re.IGNORECASE)

HELP = f"""
Trim_mp4 v{VERSION}

Select files (switches are case-insensitive: -b = -B):
  -C, --current          all mp4 files in the current directory
  -R, --recursive        all mp4 files in the current directory and below
  -F, --file PATTERN     specific file or wildcard pattern (e.g. "clips/*.mp4")

What to keep / remove (time points on the ORIGINAL video):
  -B, --begin N          kept video starts at N
  -E, --end N            kept video ends at N (the frame at N is not included)
  -D, --delete A B       remove the part from A up to B (repeatable)
  -L, --list FILE        remove segments listed in FILE ("A B" per line, # = comment)
  -T, --time s|f         values are seconds (default, decimals ok: 10.5) or
                         frame numbers (f)

Verify the cut points:
  -X, --extract          do NOT trim; instead save the frames at the cut points as
                         jpg next to the source: <name>_extract_<time>.jpg
                         (<name>_extract_f<frame>.jpg with -T f). For every cut the
                         last frame kept before it and the first frame kept after it
                         are saved (with -B only the first kept frame, with -E only
                         the last kept frame).

Other:
  -S, --subtitle FILE    burn subtitles (cannot be combined with trimming)
  -Q, --quick            stream copy: very fast, no quality loss, but cuts snap to
                         keyframes so they may be off by seconds (default is
                         frame-accurate re-encoding)
      --gpu              use NVENC (h264_nvenc) instead of libx264 for encoding
  -J, --jobs N           files processed in parallel (default 1)
  -O, --overwrite        replace the source file instead of writing <name>_trimmed.mp4
      --dryrun           show what would be done, write nothing
      --log [FILE]       also write a log file (default trim_mp4_<date>_<time>.log)
  -H, -h, -?, --help     this help

Segment file example:
  10 20
  30.5 40

Examples:
  Trim_mp4.py -F movie.mp4 -B 10.5 -E 95
  Trim_mp4.py -C -D 10 20 -D 30 40 -J 4
  Trim_mp4.py -F movie.mp4 -T f -B 240 -E 5000 -X      (check the cut frames first)
  Trim_mp4.py -R -L cuts.txt --dryrun --log
  Trim_mp4.py -F movie.mp4 -S captions.srt --gpu
"""


class VideoError(Exception):
    pass


# -------------------------------------------------
# Logging
# -------------------------------------------------

class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"{record.levelname}: {msg}"
        return msg


def setup_logging(log_arg):
    """Configure console (and optional file) logging; returns log file path or None."""
    log.setLevel(logging.DEBUG)
    log.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ConsoleFormatter())
    log.addHandler(console)

    if log_arg is None:
        return None

    path = log_arg
    if path == AUTO_LOG:
        path = datetime.datetime.now().strftime("trim_mp4_%Y%m%d_%H%M%S.log")

    try:
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        log.warning(f"Cannot create log file {path}: {e}. Continuing without log file.")
        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    return path


# -------------------------------------------------
# Utility
# -------------------------------------------------

def parse_rate(text):
    try:
        num, den = str(text).split("/")
        num, den = float(num), float(den)
        return num / den if den else 0.0
    except (ValueError, TypeError):
        return 0.0


def probe_video(path):
    cmd = ["ffprobe", "-v", "error",
           "-show_entries",
           "format=duration:stream=codec_type,r_frame_rate,avg_frame_rate,duration",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        raise VideoError(f"cannot run ffprobe: {e}")

    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        raise VideoError(lines[-1] if lines else "ffprobe failed")

    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        raise VideoError("unreadable ffprobe output")

    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        raise VideoError("no video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = None
    for candidate in (info.get("format", {}).get("duration"), video[0].get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            pass
    if not duration or duration <= 0:
        raise VideoError("cannot determine duration")

    fps = parse_rate(video[0].get("avg_frame_rate")) or parse_rate(video[0].get("r_frame_rate"))
    if fps <= 0:
        log.warning(f"{path}: cannot determine frame rate, assuming 30 fps")
        fps = 30.0

    return VideoInfo(duration, fps, has_audio)


def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def dir_writable(directory, dry):
    """True if files can be created in directory (dry run: permission check only)."""
    if dry:
        return os.access(directory, os.W_OK)
    try:
        with tempfile.TemporaryFile(dir=directory):
            pass
        return True
    except OSError:
        return False


def unique_name(path, suffix):
    base, ext = os.path.splitext(path)
    out = f"{base}{suffix}{ext}"
    i = 1
    while os.path.exists(out):
        out = f"{base}{suffix}_{i}{ext}"
        i += 1
    return out


def temp_name(out):
    base, ext = os.path.splitext(out)
    return f"{base}.~{os.getpid()}_{threading.get_ident()}{ext}"


def fmt_time(t, fps):
    return f"{t:.3f}s (f{int(round(t * fps))})"


def time_label(t, fps, mode):
    if mode == "f":
        return f"f{int(round(t * fps))}"
    return f"{t:.3f}".rstrip("0").rstrip(".") or "0"


def run_cmd(cmd, dry):
    """Run a command; returns (ok, error text)."""
    line = subprocess.list2cmdline(cmd)
    if dry:
        log.info(f"  [DRYRUN] {line}")
        return True, ""

    log.debug(f"RUN {line}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        return False, str(e)

    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        return False, " | ".join(lines[-3:]) or f"exit code {r.returncode}"
    return True, ""


def ffmpeg_base():
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def encode_args(gpu):
    if gpu:
        video = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    else:
        video = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    return video + ["-c:a", "aac", "-b:a", "192k", "-map_metadata", "0",
                    "-movflags", "+faststart"]


# -------------------------------------------------
# Planning
# -------------------------------------------------

def load_segments_from_file(path):
    """Raw (start, end) values exactly as written in the file."""
    segments = []
    with open(path, encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                raise ValueError(f"line {n}: expected 'start end', got '{line}'")
            try:
                segments.append((float(parts[0]), float(parts[1])))
            except ValueError:
                raise ValueError(f"line {n}: not a number in '{line}'")
    return segments


def plan_cuts(args, info, raw_segments):
    """Return (removed, kept) interval lists in seconds on the source timeline."""
    duration, fps = info.duration, info.fps

    def conv(v):
        return v / fps if args.time_mode == "f" else v

    removed = []

    begin = conv(args.begin) if args.begin is not None else None
    end = conv(args.end) if args.end is not None else None

    if begin is not None and begin >= duration:
        raise VideoError(f"begin ({fmt_time(begin, fps)}) is beyond the end of the video")
    if end is not None and end <= 0:
        raise VideoError("end must be greater than 0")
    if end is not None and end > duration:
        log.warning(f"end ({fmt_time(end, fps)}) is beyond the video; using the full length")
        end = None
    if begin is not None and begin > EPS:
        removed.append((0.0, begin))
    if end is not None and end < duration - EPS:
        removed.append((end, duration))

    for a, b in list(args.delete or []) + list(raw_segments):
        a, b = conv(a), conv(b)
        if a >= b or b <= 0 or a >= duration:
            log.warning(f"ignoring invalid or out-of-range segment {a:.3f}-{b:.3f}s")
            continue
        removed.append((max(0.0, a), min(duration, b)))

    removed.sort()
    merged = []
    for a, b in removed:
        if merged and a <= merged[-1][1] + EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    min_len = 0.5 / fps
    kept, cursor = [], 0.0
    for a, b in merged:
        if a - cursor >= min_len:
            kept.append((cursor, a))
        cursor = b
    if duration - cursor >= min_len:
        kept.append((cursor, duration))

    return merged, kept


# -------------------------------------------------
# Video operations
# -------------------------------------------------

def extract_frames(path, info, kept, args):
    fps, duration = info.fps, info.duration

    points = []
    for a, b in kept:
        if a > EPS:
            points.append(a)                      # first frame kept after a cut
        if b < duration - EPS:
            points.append(max(a, b - 1.0 / fps))  # last frame kept before a cut

    names = {}
    base = os.path.splitext(path)[0]
    for t in points:
        names.setdefault(f"{base}_extract_{time_label(t, fps, args.time_mode)}.jpg", t)

    outdir = os.path.dirname(os.path.abspath(path))
    if not dir_writable(outdir, args.dryrun):
        log.error(f"{path}: no write permission in {outdir}")
        return False

    ok_all = True
    for out, t in names.items():
        if os.path.exists(out) and not os.access(out, os.W_OK):
            log.error(f"{out}: exists and is write protected")
            ok_all = False
            continue

        # seek a quarter frame early so the frame at t is the first one decoded
        seek = max(0.0, t - 0.25 / fps)
        cmd = ffmpeg_base() + ["-ss", f"{seek:.6f}", "-i", path, "-map", "0:v:0",
                               "-frames:v", "1", "-q:v", "2", "-update", "1", out]
        ok, err = run_cmd(cmd, args.dryrun)
        if ok:
            verb = "would extract" if args.dryrun else "Extracted"
            log.info(f"  {verb} frame at {fmt_time(t, fps)} -> {out}")
        else:
            log.error(f"{path}: frame extraction failed at {fmt_time(t, fps)}: {err}")
            safe_remove(out)
            ok_all = False

    return ok_all


def cut_accurate(path, tmp, info, kept, args):
    fps, duration = info.fps, info.duration

    def snap(t):
        # boundaries sit a quarter frame early so float rounding can never drop a frame
        return max(0.0, t - 0.25 / fps)

    def build(gpu):
        cmd = ffmpeg_base()
        if len(kept) == 1:
            a, b = kept[0]
            if a > EPS:
                cmd += ["-ss", f"{snap(a):.6f}"]
            cmd += ["-i", path]
            if b < duration - EPS:
                cmd += ["-t", f"{snap(b) - snap(a):.6f}"]
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        else:
            chains, concat_in = [], ""
            for i, (a, b) in enumerate(kept):
                opts = []
                if a > EPS:
                    opts.append(f"start={snap(a):.6f}")
                if b < duration - EPS:
                    opts.append(f"end={snap(b):.6f}")
                trim = ":".join(opts)
                chains.append(f"[0:v:0]{'trim=' + trim + ',' if trim else ''}"
                              f"setpts=PTS-STARTPTS[v{i}]")
                concat_in += f"[v{i}]"
                if info.has_audio:
                    chains.append(f"[0:a:0]{'atrim=' + trim + ',' if trim else ''}"
                                  f"asetpts=PTS-STARTPTS[a{i}]")
                    concat_in += f"[a{i}]"
            n_audio = 1 if info.has_audio else 0
            out_labels = "[v][a]" if info.has_audio else "[v]"
            chains.append(f"{concat_in}concat=n={len(kept)}:v=1:a={n_audio}{out_labels}")
            cmd += ["-i", path, "-filter_complex", ";".join(chains), "-map", "[v]"]
            if info.has_audio:
                cmd += ["-map", "[a]"]
        return cmd + encode_args(gpu) + [tmp]

    ok, err = run_cmd(build(args.gpu), args.dryrun)
    if not ok and args.gpu:
        log.warning(f"{path}: GPU encoding failed ({err}); retrying on CPU")
        ok, err = run_cmd(build(False), args.dryrun)
    if not ok:
        log.error(f"{path}: cut failed: {err}")
    return ok


def cut_quick(path, tmp, info, kept, args):
    duration = info.duration

    def part_cmd(a, b, target):
        cmd = ffmpeg_base()
        if a > EPS:
            cmd += ["-ss", f"{a:.6f}"]
        cmd += ["-i", path]
        if b < duration - EPS:
            cmd += ["-t", f"{b - a:.6f}"]
        return cmd + ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                      "-avoid_negative_ts", "make_zero", target]

    if len(kept) == 1:
        ok, err = run_cmd(part_cmd(*kept[0], tmp), args.dryrun)
        if not ok:
            log.error(f"{path}: cut failed: {err}")
        return ok

    outdir = os.path.dirname(os.path.abspath(tmp))
    workdir = os.path.join(outdir, ".trim_work") if args.dryrun \
        else tempfile.mkdtemp(prefix=".trim_", dir=outdir)
    try:
        lines = []
        for i, (a, b) in enumerate(kept):
            part = os.path.join(workdir, f"part{i}.mp4")
            ok, err = run_cmd(part_cmd(a, b, part), args.dryrun)
            if not ok:
                log.error(f"{path}: cut failed: {err}")
                return False
            lines.append("file '" + part.replace("\\", "/").replace("'", "'\\''") + "'\n")

        listfile = os.path.join(workdir, "concat.txt")
        if not args.dryrun:
            with open(listfile, "w", encoding="utf-8") as f:
                f.writelines(lines)

        cmd = ffmpeg_base() + ["-f", "concat", "-safe", "0", "-i", listfile,
                               "-c", "copy", "-movflags", "+faststart", tmp]
        ok, err = run_cmd(cmd, args.dryrun)
        if not ok:
            log.error(f"{path}: joining parts failed: {err}")
        return ok
    finally:
        if not args.dryrun:
            shutil.rmtree(workdir, ignore_errors=True)


def do_cut(path, info, kept, args):
    out = path if args.overwrite else unique_name(path, "_trimmed")
    outdir = os.path.dirname(os.path.abspath(out))

    if args.overwrite and not os.access(path, os.W_OK):
        log.error(f"{path}: write protected, cannot overwrite")
        return False
    if not dir_writable(outdir, args.dryrun):
        log.error(f"{path}: no write permission in {outdir}")
        return False

    tmp = temp_name(out)
    try:
        cut = cut_quick if args.quick else cut_accurate
        if not cut(path, tmp, info, kept, args):
            return False

        if args.dryrun:
            log.info(f"  [DRYRUN] would write {out}")
            return True

        try:
            os.replace(tmp, out)
        except OSError as e:
            log.error(f"{path}: cannot write {out}: {e} (in use or write protected?)")
            return False

        log.info(f"Wrote {out}")
        return True
    finally:
        if not args.dryrun:
            safe_remove(tmp)


def do_subtitles(path, args):
    out = path if args.overwrite else unique_name(path, "_subtitled")
    outdir = os.path.dirname(os.path.abspath(out))

    if args.overwrite and not os.access(path, os.W_OK):
        log.error(f"{path}: write protected, cannot overwrite")
        return False
    if not dir_writable(outdir, args.dryrun):
        log.error(f"{path}: no write permission in {outdir}")
        return False

    srt = os.path.abspath(args.subtitle).replace("\\", "/").replace(":", "\\:")
    tmp = temp_name(out)

    def build(gpu):
        video = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0"] \
            if gpu else ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
        return ffmpeg_base() + ["-i", path, "-vf", f"subtitles='{srt}'"] + video + \
            ["-c:a", "copy", "-movflags", "+faststart", tmp]

    try:
        ok, err = run_cmd(build(args.gpu), args.dryrun)
        if not ok and args.gpu:
            log.warning(f"{path}: GPU encoding failed ({err}); retrying on CPU")
            ok, err = run_cmd(build(False), args.dryrun)
        if not ok:
            log.error(f"{path}: subtitle embedding failed: {err}")
            return False

        if args.dryrun:
            log.info(f"  [DRYRUN] would write {out}")
            return True

        try:
            os.replace(tmp, out)
        except OSError as e:
            log.error(f"{path}: cannot write {out}: {e} (in use or write protected?)")
            return False

        log.info(f"Wrote {out}")
        return True
    finally:
        if not args.dryrun:
            safe_remove(tmp)


# -------------------------------------------------
# File processing
# -------------------------------------------------

def process_file(path, args, raw_segments):
    try:
        return _process_file(path, args, raw_segments)
    except Exception as e:  # keep one bad file from stopping the batch
        log.error(f"{path}: unexpected error: {e!r}")
        return False


def _process_file(path, args, raw_segments):
    if not os.path.isfile(path):
        log.error(f"Missing file {path}")
        return False
    if not path.lower().endswith(".mp4"):
        log.warning(f"Skipping non-mp4 {path}")
        return False
    if not os.access(path, os.R_OK):
        log.error(f"{path}: no read permission")
        return False

    try:
        info = probe_video(path)
    except VideoError as e:
        log.error(f"{path}: incompatible video ({e})")
        return False

    log.info(f"{path}: {info.duration:.3f}s, {info.fps:.3f} fps"
             f"{'' if info.has_audio else ', no audio'}")

    if args.subtitle:
        return do_subtitles(path, args)

    try:
        removed, kept = plan_cuts(args, info, raw_segments)
    except VideoError as e:
        log.error(f"{path}: {e}")
        return False

    if not removed:
        log.info("  Nothing to cut")
        return True
    if not kept:
        log.error(f"{path}: the cuts would remove the entire video")
        return False

    for a, b in removed:
        log.info(f"  remove {fmt_time(a, info.fps)} - {fmt_time(b, info.fps)}")

    if args.extract:
        return extract_frames(path, info, kept, args)

    return do_cut(path, info, kept, args)


# -------------------------------------------------
# Batch processing
# -------------------------------------------------

def collect_files(args):
    explicit = False
    if args.filename:
        files = glob.glob(args.filename, recursive=True)
        explicit = True
    elif args.recursive:
        files = glob.glob("**/*.mp4", recursive=True)
    else:
        files = glob.glob("*.mp4")

    files = sorted({os.path.normpath(f) for f in files if os.path.isfile(f)})

    if not explicit:
        files = [f for f in files if not GENERATED.search(os.path.splitext(f)[0])]
    return files


def process_files(args, raw_segments):
    files = collect_files(args)
    if not files:
        log.error("No files found")
        return False

    start = time.time()
    exe = ThreadPoolExecutor(max(1, args.jobs))
    try:
        results = list(exe.map(lambda f: process_file(f, args, raw_segments), files))
    except KeyboardInterrupt:
        exe.shutdown(wait=False, cancel_futures=True)
        log.warning("Interrupted")
        return False
    exe.shutdown()

    ok = sum(results)
    log.info("")
    log.info(f"Processed {ok} of {len(files)}")
    log.info(f"Time {time.time() - start:.2f} sec")
    return ok == len(files)


# -------------------------------------------------
# CLI
# -------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"Error: {message}\nUse -h for help.", file=sys.stderr)
        sys.exit(2)


def build_parser():
    p = Parser(add_help=False, allow_abbrev=False)

    def opt(letter, long, **kw):
        names = [f"-{letter.upper()}", f"-{letter.lower()}"] if letter else []
        if long:
            names.append(f"--{long}")
        p.add_argument(*names, **kw)

    opt("C", "current", action="store_true", dest="current")
    opt("R", "recursive", action="store_true", dest="recursive")
    opt("F", "file", dest="filename", metavar="PATTERN")
    opt("B", "begin", type=float, dest="begin", metavar="N")
    opt("E", "end", type=float, dest="end", metavar="N")
    opt("D", "delete", nargs=2, action="append", type=float, dest="delete", metavar=("A", "B"))
    opt("L", "list", dest="segfile", metavar="FILE")
    opt("T", "time", dest="time_mode", default="s", type=str.lower, choices=["s", "f"])
    opt("X", "extract", action="store_true", dest="extract")
    opt("S", "subtitle", dest="subtitle", metavar="FILE")
    opt("Q", "quick", action="store_true", dest="quick")
    opt("J", "jobs", type=int, default=1, dest="jobs", metavar="N")
    opt("O", "overwrite", action="store_true", dest="overwrite")
    opt(None, "gpu", action="store_true", dest="gpu")
    opt(None, "dryrun", action="store_true", dest="dryrun")
    opt(None, "log", nargs="?", const=AUTO_LOG, default=None, dest="log", metavar="FILE")
    p.add_argument("-H", "-h", "-?", "--help", action="store_true", dest="help")
    return p


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except AttributeError:
        pass

    parser = build_parser()
    args = parser.parse_args()

    if args.help or len(sys.argv) == 1:
        print(HELP)
        return 0

    if not (args.filename or args.recursive or args.current):
        parser.error("select files with -F, -C or -R")
    if args.jobs < 1:
        parser.error("-J must be at least 1")

    cutting = any(v is not None for v in (args.begin, args.end, args.delete, args.segfile))
    if args.subtitle and (cutting or args.extract):
        parser.error("subtitles cannot be combined with trimming or extraction")
    if not (cutting or args.subtitle):
        parser.error("nothing to do: use -B, -E, -D, -L or -S")
    if args.begin is not None and args.begin < 0:
        parser.error("-B must not be negative")
    if args.begin is not None and args.end is not None and args.begin >= args.end:
        parser.error("-B must be smaller than -E")
    for a, b in args.delete or []:
        if a < 0 or a >= b:
            parser.error(f"invalid -D range {a:g} {b:g}")

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            parser.error(f"{tool} not found on PATH")

    log_path = setup_logging(args.log)
    log.debug(f"Trim_mp4 {VERSION} args: {' '.join(sys.argv[1:])}")
    if log_path:
        log.info(f"Logging to {log_path}")
    if args.dryrun:
        log.info("DRY RUN - nothing will be written")

    if args.subtitle:
        if not os.path.isfile(args.subtitle) or not os.access(args.subtitle, os.R_OK):
            log.error(f"Subtitle file missing or unreadable: {args.subtitle}")
            return 1
        if "'" in args.subtitle:
            log.error("Subtitle path must not contain an apostrophe")
            return 1

    raw_segments = []
    if args.segfile:
        try:
            raw_segments = load_segments_from_file(args.segfile)
        except (OSError, ValueError) as e:
            log.error(f"Segment file {args.segfile}: {e}")
            return 1

    return 0 if process_files(args, raw_segments) else 1


if __name__ == "__main__":
    sys.exit(main())
