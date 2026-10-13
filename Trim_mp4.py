import ffmpeg
import os
import glob
import argparse
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

""" Trim_mp4 version 5.0 (enhanced batch + safety features) """

# -------------------------------------------------
# Utility
# -------------------------------------------------

def probe_video(input_file):
    probe = ffmpeg.probe(input_file)

    duration = float(probe['format']['duration'])

    fps = 30
    for s in probe['streams']:
        if s['codec_type'] == 'video':
            num, den = s['r_frame_rate'].split('/')
            fps = float(num) / float(den)
            break

    return duration, fps


def convert_time(value, mode, fps):
    if value is None:
        return None

    if mode == 'f':
        return float(value) / fps

    return float(value)


def safe_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except:
        pass


def writable(path):
    try:
        with open(path, 'wb'):
            pass
        os.remove(path)
        return True
    except:
        return False

# -------------------------------------------------
# Video operations
# -------------------------------------------------

def trim_edges(input_file, output_file, begin, end):
    try:
        duration, _ = probe_video(input_file)

        if begin is None:
            begin = 0

        if end is not None:
            clip_duration = max(0, duration - begin - end)
        else:
            clip_duration = duration - begin

        (
            ffmpeg
            .input(input_file, ss=begin)
            .output(output_file, t=clip_duration, c='copy')
            .run(overwrite_output=True, quiet=True)
        )

        return True

    except Exception as e:
        print("Trim error:", e)
        return False



def delete_segments(input_file, output_file, segments):

    try:

        temp_files = []
        concat_file = "._concat.txt"

        with open(concat_file, "w") as f:

            last_end = 0
            idx = 0

            for start, end in segments:

                if start <= last_end:
                    last_end = max(last_end, end)
                    continue

                part = f"._part{idx}.mp4"

                (
                    ffmpeg
                    .input(input_file, ss=last_end, t=start-last_end)
                    .output(part, c='copy')
                    .run(overwrite_output=True, quiet=True)
                )

                f.write(f"file '{part}'\n")
                temp_files.append(part)

                idx += 1
                last_end = end

            part = f"._part{idx}.mp4"

            (
                ffmpeg
                .input(input_file, ss=last_end)
                .output(part, c='copy')
                .run(overwrite_output=True, quiet=True)
            )

            f.write(f"file '{part}'\n")
            temp_files.append(part)

        (
            ffmpeg
            .input(concat_file, format='concat', safe=0)
            .output(output_file, c='copy')
            .run(overwrite_output=True, quiet=True)
        )

        for t in temp_files:
            safe_remove(t)

        safe_remove(concat_file)

        return True

    except Exception as e:
        print("Segment delete error:", e)
        return False



def embed_subtitles_gpu(input_file, output_file, srt_file, use_gpu=True):

    if not os.path.exists(srt_file):
        print("Subtitle file missing")
        return False

    try:

        if use_gpu:
            (
                ffmpeg
                .input(input_file)
                .output(output_file,
                        vf=f"subtitles={srt_file}",
                        vcodec="h264_nvenc",
                        acodec="copy")
                .run(overwrite_output=True, quiet=True)
            )

        else:
            (
                ffmpeg
                .input(input_file)
                .output(output_file,
                        vf=f"subtitles={srt_file}")
                .run(overwrite_output=True, quiet=True)
            )

        return True

    except ffmpeg.Error:
        print("Subtitle embedding failed (codec/GPU issue)")
        return False


# -------------------------------------------------
# Segment sources
# -------------------------------------------------

def load_segments_from_file(path):

    segments = []

    if not os.path.exists(path):
        print("Segment file not found")
        return segments

    try:
        with open(path) as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith("#"):
                    continue

                parts=line.split()

                if len(parts)!=2:
                    continue

                segments.append((float(parts[0]),float(parts[1])))

    except Exception as e:
        print("Segment file error:",e)

    return segments


# -------------------------------------------------
# File processing
# -------------------------------------------------

def get_output_filename(input_file):

    base, ext = os.path.splitext(input_file)

    out = f"{base}_trimmed{ext}"

    i = 1

    while os.path.exists(out):
        out = f"{base}_trimmed_{i}{ext}"
        i += 1

    return out


def process_file(file, args):

    if not os.path.exists(file):
        print("Missing file", file)
        return False

    if not file.lower().endswith(".mp4"):
        print("Skipping non-mp4", file)
        return False

    try:
        duration, fps = probe_video(file)
    except:
        print("Incompatible video", file)
        return False

    out = file if args.overwrite else get_output_filename(file)

    if not args.overwrite and not writable(out):
        print("Cannot write", out)
        return False

    # -------------------------------------------------
    # Safety rule: subtitles cannot be combined with trimming
    # -------------------------------------------------

    if args.subtitle and (args.begin or args.end or args.delete or args.segfile):
        print("WARNING: Subtitles can not be added simultanously with video trimming")
        return False

    # -------------------------------------------------
    # Load segments
    # -------------------------------------------------

    segments = []

    if args.delete:
        for f,t in args.delete:

            start = convert_time(f,args.time_mode,fps)
            end   = convert_time(t,args.time_mode,fps)

            if start>=end:
                print("Invalid delete range",f,t)
                continue

            segments.append((start,end))

    if args.segfile:
        segments.extend(load_segments_from_file(args.segfile))

    if segments:

        segments.sort()

        if not delete_segments(file,out,segments):
            return False

        file = out

    # -------------------------------------------------
    # Edge trimming
    # -------------------------------------------------

    if args.begin or args.end:

        temp = out+"._tmp.mp4"

        if not trim_edges(
            file,
            temp,
            convert_time(args.begin,args.time_mode,fps),
            convert_time(args.end,args.time_mode,fps)
        ):
            return False

        shutil.move(temp,out)
        file = out

    # -------------------------------------------------
    # Subtitles
    # -------------------------------------------------

    if args.subtitle:

        temp = out+"._sub.mp4"

        if not embed_subtitles_gpu(file,temp,args.subtitle,args.gpu):
            return False

        shutil.move(temp,out)

    print("Processed",file)

    return True


# -------------------------------------------------
# Batch processing
# -------------------------------------------------

def process_files(args):

    files = []

    if args.filename:
        files = glob.glob(args.filename)

    elif args.recursive:
        files = glob.glob("**/*.mp4",recursive=True)

    elif args.current:
        files = glob.glob("*.mp4")

    if not files:
        print("No files found")
        return

    start=time.time()

    ok=0

    workers=max(1,args.jobs)

    with ThreadPoolExecutor(workers) as exe:
        results=list(exe.map(lambda f:process_file(f,args),files))

    ok=sum(results)

    print()
    print("Processed",ok,"of",len(files))
    print("Time",round(time.time()-start,2),"sec")


# -------------------------------------------------
# CLI
# -------------------------------------------------

def main():

    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument("-R","-r",action="store_true",dest="recursive")

    parser.add_argument("-C","-c",action="store_true",dest="current")

    parser.add_argument("-F","-f",dest="filename")

    parser.add_argument("-B",type=float,dest="begin")

    parser.add_argument("-E",type=float,dest="end")

    parser.add_argument("-D",nargs=2,action="append",type=float,dest="delete")

    parser.add_argument("-L",dest="segfile")

    parser.add_argument("-T",dest="time_mode",default="s")

    parser.add_argument("-S",dest="subtitle")

    parser.add_argument("--gpu",action="store_true",dest="gpu")

    parser.add_argument("-J",type=int,default=1,dest="jobs")

    parser.add_argument("-O",action="store_true",dest="overwrite")

    parser.add_argument("-h","-H","-?",action="store_true",dest="help")

    args=parser.parse_args()

    args.time_mode=str(args.time_mode).lower()

    if args.help:
        print("""
Trim_mp4 v5

-C             current directory
-R             recursive
-F file        specific file

-B n           trim beginning
-E n           trim end

-D f t         delete segment (repeatable)
-L file        delete segments from file

-T s|f         seconds or frames

-S file        burn subtitles (exclusive mode)
--gpu          GPU encoding for subtitles

-J n           parallel jobs

-O             overwrite source

Example segment file:

10 20
30 40

Example usage:

parallel processing
-J 4 -C

remove segments from file
-L cuts.txt

subtitles only
-S captions.srt --gpu
""")
        return

    process_files(args)


if __name__=="__main__":
    main()
