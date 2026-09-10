"""
Unit and integration tests for the digest pipeline.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import digest as prepare_mod
import frames as crop_mod
import frames as dedupe_mod
import slide_materials as slides_mod
import transcript as norm_mod


def compile_args(*arguments):
    return prepare_mod.build_parser().parse_args([str(a) for a in arguments])


def compile_text(path, output, title=""):
    prepare_mod.compile_package(compile_args("-t", path, "-o", output, "--title", title))


class TestSlideMaterials(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="digest_slides_"))
        self.source_pdf = self.temp_dir / "professor-slides.pdf"
        self.source_pdf.write_bytes(b"placeholder PDF used by the ingestion contract test")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ingests_pdf_without_transcript(self):
        pages = [
            slides_mod.SlidePage(1, "Gerência de configuração e baseline"),
            slides_mod.SlidePage(2, "Imagem sem camada de texto"),
        ]

        with patch.object(slides_mod, "extract_slide_pages", return_value=pages), patch.object(
            slides_mod, "render_slide_pages", return_value=[]
        ):
            count = slides_mod.ingest_slides(self.source_pdf, self.temp_dir / "lecture")

        materials = self.temp_dir / "lecture" / "materials"
        self.assertEqual(count, 2)
        self.assertTrue((materials / "slides.pdf").is_file())
        self.assertIn("## Página 1", (materials / "slides.md").read_text(encoding="utf-8"))
        self.assertIn("2,", (materials / "slides-index.csv").read_text(encoding="utf-8"))
        self.assertIn("slides.pdf", (materials / "README.md").read_text(encoding="utf-8"))
        self.assertIn("digest.py --transcript", (self.temp_dir / "lecture" / "README.md").read_text(encoding="utf-8"))

    def test_links_transcript_sections_to_page_candidates(self):
        pages = [
            slides_mod.SlidePage(1, "Gerência de configuração baseline item de configuração"),
            slides_mod.SlidePage(2, "Integração contínua pipeline deploy"),
        ]
        entries = [{"type": "timestamp", "sec": 42, "text": "A baseline identifica o item de configuração."}]

        links = slides_mod.link_transcript_to_slides(entries, pages)
        rendered = slides_mod.render_slide_links(links)

        self.assertEqual(links[0]["timestamp"], 42)
        self.assertEqual(links[0]["candidates"][0]["page"], 1)
        self.assertIn("confiança", rendered)
        self.assertIn("não prova", rendered)

    LIBREOFFICE_SHIM = (
        'outdir=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in --outdir) outdir="$2"; shift 2;; *) last="$1"; shift;; esac\n'
        'done\n'
        'printf "converted PDF" > "$outdir/$(basename "$last" .pptx).pdf"'
    )

    def _fake_soffice(self, body: str):
        """Stands in for LibreOffice so the conversion wiring is exercised for real."""
        script = self.temp_dir / "fake-soffice"
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return patch.object(slides_mod, "_find_soffice", return_value=str(script))

    def _source_pptx(self) -> Path:
        deck = self.temp_dir / "professor-slides.pptx"
        deck.write_bytes(b"placeholder PPTX used by the conversion contract test")
        return deck

    def test_ingests_pptx_by_converting_to_pdf(self):
        deck = self._source_pptx()
        pages = [slides_mod.SlidePage(1, "Gerência de configuração e baseline")]

        with self._fake_soffice(self.LIBREOFFICE_SHIM), patch.object(
            slides_mod, "extract_slide_pages", return_value=pages
        ), patch.object(slides_mod, "render_slide_pages", return_value=[]):
            count = slides_mod.ingest_slides(deck, self.temp_dir / "lecture")

        materials = self.temp_dir / "lecture" / "materials"
        readme = (materials / "README.md").read_text(encoding="utf-8")
        self.assertEqual(count, 1)
        self.assertEqual((materials / "slides.pdf").read_bytes(), b"converted PDF")
        self.assertEqual((materials / "slides.pptx").read_bytes(), deck.read_bytes())
        self.assertIn("professor-slides.pptx", readme)
        self.assertIn("LibreOffice", readme)
        self.assertIn("## Página 1", (materials / "slides.md").read_text(encoding="utf-8"))

    def test_reingesting_a_pdf_drops_the_previous_pptx_source(self):
        materials = self.temp_dir / "lecture" / "materials"
        materials.mkdir(parents=True)
        (materials / "slides.pptx").write_bytes(b"deck from an earlier ingestion")
        pages = [slides_mod.SlidePage(1, "Baseline")]

        with patch.object(slides_mod, "extract_slide_pages", return_value=pages), patch.object(
            slides_mod, "render_slide_pages", return_value=[]
        ):
            slides_mod.ingest_slides(self.source_pdf, self.temp_dir / "lecture")

        self.assertFalse((materials / "slides.pptx").exists())

    def test_rejects_unsupported_slide_material(self):
        keynote = self.temp_dir / "slides.key"
        keynote.touch()

        with self.assertRaisesRegex(ValueError, r"\.pptx"):
            slides_mod.ingest_slides(keynote, self.temp_dir / "lecture")

    def test_reports_missing_libreoffice_without_writing_a_package(self):
        deck = self._source_pptx()

        with patch.object(slides_mod, "_find_soffice", return_value=None), self.assertRaisesRegex(
            ValueError, "LibreOffice is required"
        ):
            slides_mod.ingest_slides(deck, self.temp_dir / "lecture")

        self.assertFalse((self.temp_dir / "lecture" / "materials").exists())

    def test_failed_conversion_leaves_no_package(self):
        deck = self._source_pptx()

        with self._fake_soffice('echo "source file could not be loaded" >&2\nexit 1'), self.assertRaisesRegex(
            ValueError, "could not be loaded"
        ):
            slides_mod.ingest_slides(deck, self.temp_dir / "lecture")

        self.assertFalse((self.temp_dir / "lecture" / "materials").exists())

    def test_silent_conversion_without_output_is_an_error(self):
        deck = self._source_pptx()

        with self._fake_soffice("exit 0"), self.assertRaisesRegex(ValueError, "no PDF"):
            slides_mod.ingest_slides(deck, self.temp_dir / "lecture")

        self.assertFalse((self.temp_dir / "lecture" / "materials").exists())

    def test_stalled_conversion_times_out(self):
        deck = self._source_pptx()

        with self._fake_soffice("sleep 5"), patch.object(
            slides_mod, "CONVERSION_TIMEOUT_SECONDS", 1
        ), self.assertRaisesRegex(ValueError, "did not finish converting"):
            slides_mod.ingest_slides(deck, self.temp_dir / "lecture")

        self.assertFalse((self.temp_dir / "lecture" / "materials").exists())

    def test_compiled_readme_mentions_ingested_slides(self):
        readme = prepare_mod.generate_readme(
            lecture_title="Aula com slides",
            offsets_str=["00:00:00"],
            offsets_seconds=[0],
            crop_boxes={},
            has_slides=True,
        )

        self.assertIn("materials/slides.pdf", readme)
        self.assertIn("slide-links.md", readme)
        self.assertIn("not proof", readme)


class TestTranscriptOnlyPackage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="digest_transcript_only_"))
        self.transcript = self.temp_dir / "transcript.txt"
        self.transcript.write_text("[00:00] Gerência de configuração e baseline.", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_prepares_without_recording(self):
        output = self.temp_dir / "lecture"

        compile_text(self.transcript, output, "Aula sem gravação")

        self.assertTrue((output / "transcript.md").is_file())
        self.assertTrue((output / "README.md").is_file())
        self.assertFalse((output / "frames").exists())
        self.assertIn("transcript-only", (output / "README.md").read_text(encoding="utf-8"))

    def test_links_ingested_slides_without_recording(self):
        output = self.temp_dir / "lecture"
        (output / "materials").mkdir(parents=True)
        (output / "materials" / "slides.pdf").write_bytes(b"placeholder PDF")

        with patch.object(slides_mod, "extract_slide_pages", return_value=[
            slides_mod.SlidePage(1, "Gerência de configuração e baseline")
        ]):
            compile_text(self.transcript, output)

        self.assertTrue((output / "materials" / "slide-links.md").is_file())
        readme = (output / "README.md").read_text(encoding="utf-8")
        self.assertIn("materials/slides.pdf", readme)


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

    def test_wispr_pause_markers_are_preserved(self):
        wispr_sample = """[00:00] Speaker 1: First segment.
[7:57 PM] -- Paused --
[8:01 PM] -- Resumed --
[04:37] Speaker 1: Second segment with explanation.
"""
        entries = norm_mod.normalize_transcript(wispr_sample)
        md = norm_mod.generate_markdown(entries)

        self.assertEqual(entries[0]["sec"], 0)
        self.assertEqual(entries[-1]["sec"], 277)
        self.assertIn("Paused", md)
        self.assertIn("Resumed", md)
        self.assertIn("## 00:04:37", md)

    def test_plain_wispr_text_without_timestamp_starts_at_zero(self):
        entries = norm_mod.normalize_transcript("Speaker 1: Welcome to class.")

        self.assertEqual(entries, [{"type": "timestamp", "sec": 0, "text": "Speaker 1: Welcome to class."}])

    def test_backward_wispr_timestamps_require_a_corrected_source(self):
        wispr_sample = """[00:10:00] Speaker 1: End of the first segment.
[00:00:00] Speaker 1: Wispr restarted after a pause.
"""

        with self.assertRaisesRegex(ValueError, "timestamps go backwards"):
            norm_mod.normalize_transcript(wispr_sample)

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


class TestInputContract(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="digest_inputs_"))
        self.recording = self.temp_dir / "recording.mov"
        self.transcript = self.temp_dir / "transcript.txt"
        self.recording.touch()
        self.transcript.write_text("[00:00] Test", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rejects_non_txt_transcript(self):
        source = self.temp_dir / "transcript.srt"
        source.touch()
        with self.assertRaisesRegex(ValueError, r"\.txt"):
            compile_text(source, self.temp_dir / "output")

    def test_rejects_missing_offset_before_creating_output(self):
        second = self.temp_dir / "recording-2.mov"
        second.touch()
        output = self.temp_dir / "output"
        with self.assertRaisesRegex(ValueError, "one --offsets value per recording"):
            prepare_mod.compile_package(compile_args(self.recording, second, "-t", self.transcript,
                                                     "--offsets", "0", "-o", output))
        self.assertFalse(output.exists())

    def test_rejects_wispr_timestamp_reset_before_creating_output(self):
        self.transcript.write_text("[00:10:00] First.\n[00:00:00] Restarted.", encoding="utf-8")
        output = self.temp_dir / "output"
        with self.assertRaisesRegex(ValueError, "timestamps go backwards"):
            compile_text(self.transcript, output)
        self.assertFalse(output.exists())


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
        self.temp_dir = Path(tempfile.mkdtemp(prefix="digest_crop_"))

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
        cls.temp_dir = Path(tempfile.mkdtemp(prefix="digest_test_"))
        cls.video_path = cls.temp_dir / "recording.mov"
        cls.transcript_path = cls.temp_dir / "transcript.txt"

        # Generate a synthetic 15-second test video using ffmpeg
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "testsrc=duration=15:size=640x360:rate=10",
            "-pix_fmt", "yuv420p",
            str(cls.video_path)
        ]
        res = subprocess.run(cmd, capture_output=True, check=False)
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
        materials_dir = out_lecture_dir / "materials"
        materials_dir.mkdir(parents=True)
        (materials_dir / "slides.pdf").write_bytes(b"placeholder PDF supplied by the earlier ingestion step")

        # Run prepare_lecture with offset 04:37 (277 seconds)
        fake_pages = [slides_mod.SlidePage(1, "Algorithms complexity chart")]
        with patch.object(slides_mod, "extract_slide_pages", return_value=fake_pages):
            prepare_mod.compile_package(compile_args(
                self.video_path, "-t", self.transcript_path, "-o", out_lecture_dir,
                "--offsets", "00:04:37", "--max-edge", "320",
                "--title", "Algorithms Lecture 01",
            ))

        # Check deliverables
        self.assertTrue((out_lecture_dir / "README.md").is_file())
        self.assertTrue((out_lecture_dir / "transcript.md").is_file())
        self.assertTrue((out_lecture_dir / "frames").is_dir())
        self.assertTrue((out_lecture_dir / "frames" / "index.csv").is_file())
        self.assertTrue((out_lecture_dir / "materials" / "slides.pdf").is_file())
        self.assertTrue((out_lecture_dir / "materials" / "slide-links.md").is_file())
        # o pacote é fonte read-only: o vault é o destino, não uma pasta output/
        self.assertFalse((out_lecture_dir / "output").exists())

        # Check README content
        readme_text = (out_lecture_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("Algorithms Lecture 01", readme_text)
        self.assertIn("00:04:37", readme_text)
        self.assertIn("frames/00-18-42.jpg", readme_text)
        self.assertIn("index.csv", readme_text)
        self.assertIn("What the frames show", readme_text)
        self.assertIn("<vault>/raw/lectures", readme_text)
        self.assertIn("materials/slides.pdf", readme_text)
        self.assertNotIn("/Users/", readme_text)

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
