# video-clip

Batch TS -> MP4 processor with subtitle cropping, scaling, resumable task tracking, and quick validation mode.

## Workflow

1. Edit the top of `process_videos.py`:
   - `input_dir`: your TS source folder
   - `output_dir`: your MP4 output folder
   - `workers`: start with about half your physical cores

2. Run a quick validation pass:

```bash
python process_videos.py --input /data/ts --output /data/mp4_test --mode fast --limit 10
```

This uses NVENC and only processes a few files so you can inspect the output MP4s first.

3. Open a few generated MP4 files and confirm:
   - subtitles are fully removed
   - the crop is correct
   - there is no unexpected cut-off

   The confirmed crop geometry is:

```text
crop=1920:980:0:0,scale=1920:1080
```

4. Run the full production batch:

```bash
python process_videos.py --input /data/ts --output /data/mp4
```

5. Check progress at any time:

```bash
python process_videos.py --input /data/ts --output /data/mp4 --status
```

6. If the process is interrupted:
   - press `Ctrl+C`
   - rerun the same command later
   - completed files are skipped automatically

## Ten-machine deployment

For 10 Windows PCs, the simplest setup is shard-based fan-out:

1. Put the TS source folder on a shared location all PCs can read, or copy the same source folder to each machine.
2. Give each machine its own output folder and its own SQLite DB.
3. Assign shard indexes `0` through `9`.

Example on machine 0:

```bash
python process_videos.py --input \\server\share\ts --output D:\out\mp4_0 --shard-index 0 --shard-count 10
```

Machine 1 uses `--shard-index 1`, and so on up to `9`.

This keeps the deployment simple:

- no shared database contention
- no central scheduler required
- easy to rerun a single machine
- failed files stay isolated to that machine's DB

## Notes

- `--mode fast` defaults to `--limit 10` unless you override it.
- Failed files are marked `failed` in SQLite and do not block the rest.
- `--status` shows pending, done, and failed counts plus the failed file list.
- The script looks for `ffmpeg` on `PATH` first, then in `./tools/ffmpeg`.
- The subtitle crop is fixed at 100px from the bottom, which keeps a 3px safety margin above the confirmed subtitle band.
- For a 10-machine batch, shard the source with `--shard-index` and `--shard-count` instead of sharing one SQLite DB across machines.

## Packaging

Build a portable Windows package:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
```

The package is created at:

- `dist\VideoBatchProcessor\`
- `release\VideoBatchProcessor-win-portable.zip`

The portable folder contains:

- `VideoBatchProcessor.exe`
- `config.json`
- `tools\ffmpeg\`
- `run.cmd`

For deployment, unzip the archive on each machine and edit `config.json` only.
