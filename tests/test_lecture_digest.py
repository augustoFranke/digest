#!/usr/bin/env python3
"""
Unit and integration tests for lecture-digest V0 pipeline.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from importlib.machinery import SourceFileLoader

import numpy as np
from PIL import Image

# Load modules dynamically
root_dir = Path(__file__).parent.parent.resolve()
norm_mod = SourceFileLoader("norm_mod", str(root_dir / "01_normalize_transcript.py")).load_module()
extract_mod = SourceFileLoader("extract_mod", str(root_dir / "02_extract_frames.py")).load_module()
crop_mod = SourceFileLoader("crop_mod", str(root_dir / "03_crop_frames.py")).load_module()
dedupe_mod = SourceFileLoader("dedupe_mod", str(root_dir / "04_dedupe_and_rename.py")).load_module()
prepare_mod = SourceFileLoader("prepare_mod", str(root_dir / "prepare_lecture.py")).load_module()


class TestTranscriptNormalization(unittest.TestCase):
    def test_parse_timestamp_to_seconds(self):
        self.assertEqual(norm_mod.parse_timestamp_to_seconds("04:37"), 277)
        self.assertEqual(norm_mod.parse_timestamp_to_seconds("00:04:37"), 277)
        self.assertEqual(norm_mod.parse_timestamp_to_seconds("01:04:37"), 3877)
        self.assertEqual(norm_mod.parse_timestamp_to_seconds("00:04:37,500"), 277)
        self.assertEqual(norm_mod.parse_timestamp_to_seconds("00:04:37.500"), 277)

    def test_format_seconds_to_timestamp(self):
        self.assertEqual(norm_mod.format_seconds_to_timestamp(277), "00:04:37")
        self.assertEqual(norm_mod.format_seconds_to_timestamp(3661), "01:01:01")

    def test_srt_parsing(self):
        srt_sample = """1
00:00:00,000 --> 00:00:03,000
Welcome to class.

2
00:04:37,500 --> 00:04:42,000
Look at this code example.
"""
        entries = norm_mod.parse_srt(srt_sample)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["sec"], 0)
        self.assertEqual(entries[0]["text"], "Welcome to class.")
        self.assertEqual(entries[1]["sec"], 277)
        self.assertEqual(entries[1]["text"], "Look at this code example.")

        md = norm_mod.generate_markdown(entries)
        self.assertIn("# Transcript", md)
        self.assertIn("## 00:00:00", md)
        self.assertIn("## 00:04:37", md)
        self.assertIn("Look at this code example.", md)

    def test_vtt_parsing(self):
        vtt_sample = """WEBVTT

00:00.000 --> 00:04.000
First segment.

04:37.000 --> 04:45.000
Second segment with explanation.
"""
        entries = norm_mod.parse_vtt(vtt_sample)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["sec"], 0)
        self.assertEqual(entries[0]["text"], "First segment.")
        self.assertEqual(entries[1]["sec"], 277)
        self.assertEqual(entries[1]["text"], "Second segment with explanation.")

    def test_timestamped_text_parsing(self):
        text_sample = """
[00:00:00] Professor: Welcome everyone.

04:37 - Look at this slide here.
We can see the algorithm complexity is O(N).

05:18 Professor: Next section.
"""
        entries = norm_mod.normalize_transcript(text_sample)
        self.assertGreaterEqual(len(entries), 3)
        self.assertEqual(entries[0]["sec"], 0)
        self.assertEqual(entries[1]["sec"], 277)
        self.assertEqual(entries[2]["sec"], 318)


class TestDeduplicationAndRenaming(unittest.TestCase):
    def test_offset_parsing(self):
        self.assertEqual(dedupe_mod.parse_offset_string("00:04:37"), 277)
        self.assertEqual(dedupe_mod.parse_offset_string("04:37"), 277)
        self.assertEqual(dedupe_mod.parse_offset_string("277"), 277)
        self.assertEqual(dedupe_mod.parse_offset_string("+277"), 277)
        self.assertEqual(dedupe_mod.parse_offset_string("+00:04:37"), 277)

    def test_filename_formatting(self):
        self.assertEqual(dedupe_mod.format_seconds_to_filename(277), "00-04-37.jpg")
        self.assertEqual(dedupe_mod.format_seconds_to_filename(292), "00-04-52.jpg")
        self.assertEqual(dedupe_mod.format_seconds_to_filename(3661), "01-01-01.jpg")


def write_synthetic_frames(directory, count, content_box, size=(800, 600), prefix="frame", seed=0):
    """
    Writes frames that imitate a shared-screen recording: everything outside
    `content_box` is identical in every frame (the call's interface), everything
    inside changes (the lecture).
    """
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    width, height = size
    left, top, right, bottom = content_box

    chrome = np.zeros((height, width, 3), dtype=np.uint8)
    chrome[:, :] = (30, 30, 30)
    chrome[0:40, :] = (200, 200, 200)          # window title bar
    chrome[:, width - 60:] = (120, 120, 120)   # participant strip
    chrome[height - 50:, :] = (90, 90, 90)     # control bar

    paths = []
    for i in range(count):
        frame = chrome.copy()
        # Coarse blocks, not per-pixel noise: a screen changes in windows and
        # blocks of text, and that is what survives the downscale the detector
        # analyses at. Per-pixel noise would average away to nothing.
        blocks = rng.integers(0, 256, (6, 8, 3), dtype=np.uint8)
        content = np.asarray(
            Image.fromarray(blocks).resize((right - left, bottom - top), Image.NEAREST)
        )
        frame[top:bottom, left:right] = content
        path = directory / f"{prefix}_{i + 1:06d}.jpg"
        Image.fromarray(frame).save(path, quality=90)
        paths.append(path)
    return paths


class TestFrameCropping(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="lecture_digest_crop_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_parse_box_string(self):
        self.assertEqual(crop_mod.parse_box_string("10,20,300,400"), (10, 20, 300, 400))
        self.assertEqual(crop_mod.parse_box_string(" 10 , 20 , 300 , 400 "), (10, 20, 300, 400))
        for bad in ["10,20,30", "10,20,5,400", "10,20,300,15", "a,b,c,d"]:
            with self.assertRaises(ValueError):
                crop_mod.parse_box_string(bad)

    def test_detects_the_changing_region(self):
        box = (200, 150, 600, 450)
        frames = write_synthetic_frames(self.temp_dir / "raw", 12, box)

        detected = crop_mod.detect_content_box(frames, verbose=False)

        self.assertIsNotNone(detected, "The changing region should have been detected.")
        for got, want, side in zip(detected, box, ("left", "top", "right", "bottom")):
            self.assertLessEqual(abs(got - want), 16, f"{side} edge off by more than one analysis cell")

    def test_static_frames_are_not_cropped(self):
        frames = write_synthetic_frames(self.temp_dir / "raw", 8, (200, 150, 600, 450), seed=1)
        # Overwrite every frame with a copy of the first, leaving nothing that moves.
        for f in frames[1:]:
            shutil.copyfile(frames[0], f)

        self.assertIsNone(crop_mod.detect_content_box(frames, verbose=False))

    def test_too_few_frames_are_not_cropped(self):
        frames = write_synthetic_frames(self.temp_dir / "raw", 3, (200, 150, 600, 450))
        self.assertIsNone(crop_mod.detect_content_box(frames, verbose=False))

    def test_implausibly_large_region_is_rejected(self):
        frames = write_synthetic_frames(self.temp_dir / "raw", 10, (0, 0, 800, 600))
        self.assertIsNone(crop_mod.detect_content_box(frames, verbose=False))

    def test_faint_neighbour_is_trimmed_off_the_edge(self):
        """A live participant tile beside the shared screen must not widen the crop."""
        raw = self.temp_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(7)
        content = (200, 150, 600, 450)
        tile = (630, 150, 720, 450)

        frames = []
        for i in range(12):
            frame = np.full((600, 800, 3), 30, dtype=np.uint8)
            blocks = rng.integers(0, 256, (6, 8, 3), dtype=np.uint8)
            frame[content[1]:content[3], content[0]:content[2]] = np.asarray(
                Image.fromarray(blocks).resize((content[2] - content[0], content[3] - content[1]), Image.NEAREST)
            )
            # The tile moves too, but with a fraction of the amplitude.
            faint = rng.integers(112, 144, (6, 4, 3), dtype=np.uint8)
            frame[tile[1]:tile[3], tile[0]:tile[2]] = np.asarray(
                Image.fromarray(faint).resize((tile[2] - tile[0], tile[3] - tile[1]), Image.NEAREST)
            )
            path = raw / f"frame_{i + 1:06d}.jpg"
            Image.fromarray(frame).save(path, quality=90)
            frames.append(path)

        detected = crop_mod.detect_content_box(frames, verbose=False)

        self.assertIsNotNone(detected)
        self.assertLessEqual(detected[2], tile[0], "The faint tile was swallowed into the crop.")
        self.assertGreaterEqual(detected[2], content[2] - 16, "The crop cut into the content.")

    def test_meet_layout_keeps_only_the_shared_screen(self):
        """
        The layout that broke the first detector: participant tiles beside the
        shared screen that are not faint at all, plus a clock and a control bar
        that come and go. Everything there moves; only the shared screen keeps
        redrawing itself, and only it may survive the crop.
        """
        raw = self.temp_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(11)
        width, height = 1568, 910
        content = (16, 124, 1144, 764)
        tiles = (1160, 176, 1560, 700)
        top_bar = 56
        control_bar = (520, 845, 1060, 900)

        def blocks(shape, box, low=0, high=256):
            patch = rng.integers(low, high, shape, dtype=np.uint8)
            return np.asarray(
                Image.fromarray(patch).resize((box[2] - box[0], box[3] - box[1]), Image.NEAREST)
            )

        frames = []
        for i in range(24):
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[content[1]:content[3], content[0]:content[2]] = blocks((8, 10, 3), content)
            # A 3x3 grid of webcam tiles: each keeps its own colour and wobbles
            # around it, which is what a face in front of a wall looks like.
            for row in range(3):
                for col in range(3):
                    tile_w = (tiles[2] - tiles[0]) // 3
                    tile_h = (tiles[3] - tiles[1]) // 3
                    left = tiles[0] + col * tile_w
                    top = tiles[1] + row * tile_h
                    base = 70 + 18 * (row * 3 + col)
                    box = (left + 4, top + 4, left + tile_w - 4, top + tile_h - 4)
                    frame[box[1]:box[3], box[0]:box[2]] = blocks((4, 4, 3), box, base - 35, base + 35)
            # The clock in the call's top bar, one changing digit group.
            frame[8:top_bar - 8, 16:160] = blocks((1, 4, 3), (16, 8, 160, top_bar - 8), 180, 256)
            # The control bar is only drawn while the pointer is near it: a dark
            # pill fading in and out of an almost-black background.
            frame[control_bar[1]:control_bar[3], control_bar[0]:control_bar[2]] = 64 if i % 3 == 0 else 24
            # Once in the hour the whole screen changes at once, because the
            # presenter went full-screen and came back. That single event is
            # what defeats a detector measuring spread over the whole recording:
            # it lifts every pixel's deviation, letterbox and chrome included.
            if i == 20:
                frame[:, :] = blocks((6, 8, 3), (0, 0, width, height))
            path = raw / f"frame_{i + 1:06d}.jpg"
            Image.fromarray(frame).save(path, quality=90)
            frames.append(path)

        detected = crop_mod.detect_content_box(frames, verbose=False)

        self.assertIsNotNone(detected, "The shared screen should have been detected.")
        left, top, right, bottom = detected
        self.assertLessEqual(right, tiles[0], "The participant tiles were swallowed into the crop.")
        self.assertGreaterEqual(top, top_bar, "The call's top bar was swallowed into the crop.")
        self.assertLessEqual(bottom, control_bar[1], "The control bar was swallowed into the crop.")
        for got, want, side in zip(detected, content, ("left", "top", "right", "bottom")):
            self.assertLessEqual(abs(got - want), 16, f"{side} edge off by more than one analysis cell")

    def test_crop_and_resize_fits_the_long_edge(self):
        frames = write_synthetic_frames(self.temp_dir / "raw", 4, (200, 150, 600, 450))
        dest = self.temp_dir / "out.jpg"

        crop_mod.crop_and_resize(frames[0], dest, (200, 150, 600, 450), max_edge=200, quality=80)

        with Image.open(dest) as img:
            self.assertEqual(max(img.size), 200)
            self.assertEqual(img.size, (200, 150))  # 400x300 halved, aspect ratio preserved

    def test_crop_and_resize_does_not_upscale(self):
        frames = write_synthetic_frames(self.temp_dir / "raw", 4, (200, 150, 600, 450))
        dest = self.temp_dir / "out.jpg"

        crop_mod.crop_and_resize(frames[0], dest, (200, 150, 600, 450), max_edge=5000, quality=80)

        with Image.open(dest) as img:
            self.assertEqual(img.size, (400, 300))

    def test_each_video_gets_its_own_crop(self):
        raw = self.temp_dir / "raw"
        write_synthetic_frames(raw, 10, (200, 150, 600, 450), prefix="vid1_frame", seed=2)
        write_synthetic_frames(raw, 10, (100, 100, 700, 500), prefix="vid2_frame", seed=3)

        boxes = crop_mod.crop_frames(raw, self.temp_dir / "cropped", max_edge=0, quality=80)

        self.assertEqual(sorted(boxes), [0, 1])
        self.assertLessEqual(abs(boxes[0][0] - 200), 16)
        self.assertLessEqual(abs(boxes[1][0] - 100), 16)
        self.assertEqual(len(list((self.temp_dir / "cropped").glob("*.jpg"))), 20)

    def test_explicit_box_overrides_detection(self):
        raw = self.temp_dir / "raw"
        write_synthetic_frames(raw, 10, (200, 150, 600, 450), seed=4)

        boxes = crop_mod.crop_frames(raw, self.temp_dir / "cropped", max_edge=0, quality=80, box=(0, 0, 100, 80))

        self.assertEqual(boxes[0], (0, 0, 100, 80))
        with Image.open(next((self.temp_dir / "cropped").glob("*.jpg"))) as img:
            self.assertEqual(img.size, (100, 80))

    def test_cropping_shrinks_the_frames_on_disk(self):
        raw = self.temp_dir / "raw"
        frames = write_synthetic_frames(raw, 10, (200, 150, 600, 450), seed=5)
        before = sum(f.stat().st_size for f in frames)

        crop_mod.crop_frames(raw, self.temp_dir / "cropped", max_edge=200, quality=80)

        after = sum(f.stat().st_size for f in (self.temp_dir / "cropped").glob("*.jpg"))
        self.assertLess(after, before)


class TestEndToEndPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = Path(tempfile.mkdtemp(prefix="lecture_digest_test_"))
        cls.video_path = cls.temp_dir / "recording.mp4"
        cls.transcript_path = cls.temp_dir / "transcript.txt"

        # Generate a synthetic 15-second test video using ffmpeg
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "testsrc=duration=15:size=640x360:rate=10",
            "-pix_fmt", "yuv420p",
            str(cls.video_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg test video generation failed: {res.stderr.decode()}")

        # Create sample transcript
        cls.transcript_path.write_text(
            """[00:00:00] Introduction to the course.
[00:04:37] Look at this chart on the screen.
[00:04:52] We can see the blue state.
""",
            encoding="utf-8"
        )

    @classmethod
    def tearDownClass(cls):
        if cls.temp_dir.exists():
            shutil.rmtree(cls.temp_dir)

    def test_prepare_lecture_e2e(self):
        out_lecture_dir = self.temp_dir / "lecture_output"

        # Run prepare_lecture with offset 04:37 (277 seconds)
        prepare_mod.prepare_lecture(
            video_paths=[self.video_path],
            transcript_path=self.transcript_path,
            output_dir=out_lecture_dir,
            offsets=["00:04:37"],
            interval_seconds=5.0,
            phash_threshold=6,
            max_interval_seconds=30.0,
            quality=2,
            max_edge=320,
            keep_raw=False,
            title="Algorithms Lecture 01"
        )

        # Check deliverables
        self.assertTrue((out_lecture_dir / "README.md").is_file())
        self.assertTrue((out_lecture_dir / "transcript.md").is_file())
        self.assertTrue((out_lecture_dir / "frames").is_dir())
        self.assertTrue((out_lecture_dir / "frames" / "index.csv").is_file())
        # o pacote é fonte read-only: o vault é o destino, não uma pasta output/
        self.assertFalse((out_lecture_dir / "output").exists())

        # Check README content
        readme_text = (out_lecture_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("Algorithms Lecture 01", readme_text)
        self.assertIn("00:04:37", readme_text)
        self.assertIn("frames/00-18-42.jpg", readme_text)
        self.assertIn("index.csv", readme_text)
        self.assertIn("What the frames show", readme_text)

        # Check transcript.md content
        transcript_md = (out_lecture_dir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("# Transcript", transcript_md)
        self.assertIn("## 00:04:37", transcript_md)
        self.assertIn("Look at this chart on the screen.", transcript_md)

        # Check frames
        frames = list((out_lecture_dir / "frames").glob("*.jpg"))
        self.assertGreater(len(frames), 0)
        first_frame_path = out_lecture_dir / "frames" / "00-04-37.jpg"
        self.assertTrue(first_frame_path.is_file(), f"Expected {first_frame_path} to exist.")

        # Frames are resized, and the intermediate directories are gone.
        for frame in frames:
            with Image.open(frame) as img:
                self.assertLessEqual(max(img.size), 320)
        self.assertFalse((out_lecture_dir / "frames" / "raw").exists())
        self.assertFalse((out_lecture_dir / "frames" / "cropped").exists())

        # Check index.csv
        index_csv = (out_lecture_dir / "frames" / "index.csv").read_text(encoding="utf-8")
        self.assertIn("timestamp,file", index_csv)
        self.assertIn("00:04:37,00-04-37.jpg", index_csv)


if __name__ == "__main__":
    unittest.main()
