"""Public CLI contracts and real FFmpeg integration, without model downloads."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pypdf import PdfWriter

import digest
import transcript


@contextlib.contextmanager
def installed_backend(backend):
    previous = sys.modules.get("faster_whisper")
    existed = "faster_whisper" in sys.modules
    sys.modules["faster_whisper"] = backend
    try:
        yield
    finally:
        if existed:
            sys.modules["faster_whisper"] = previous
        else:
            sys.modules.pop("faster_whisper", None)


def arguments(*values):
    return digest.build_parser().parse_args([str(value) for value in values])


class TestModes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="digest_modes_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.text = self.root / "text.txt"
        self.text.write_text("[00:00] Algorithms and complexity.\n[00:10] Next step.")

    def compile(self, *values):
        digest.compile_package(arguments(*values, "-o", self.output))

    def test_cli_normalizes_text_without_audio_dependencies(self):
        result = subprocess.run(
            [sys.executable, "digest.py", "-t", str(self.text), "-o", str(self.output)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## 00:00:10", (self.output / "transcript.md").read_text())
        self.assertFalse((self.output / "frames").exists())
        self.assertIn("Supplied text", (self.output / "README.md").read_text())

    def test_invalid_combinations_fail_before_output(self):
        for flags in (
            [],
            ["--transcribe"],
            ["-t", self.text, "--no-frames"],
            ["-t", self.text, "--offsets", "0"],
            ["-t", self.text, "--model", "tiny"],
            ["-t", self.text, "--language", "en"],
        ):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.compile(*flags)
            self.assertFalse(self.output.exists())

    def test_mutually_exclusive_sources_and_crops(self):
        for flags in (
            ["-t", self.text, "--transcribe"],
            ["-t", self.text, "--crop-box", "0,0,20,20", "--no-crop"],
        ):
            with (
                self.subTest(flags=flags),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                arguments(*flags, "-o", self.output)

    def test_invalid_numeric_flags_fail_before_output(self):
        for name, value in (
            ("--interval", "nan"),
            ("--interval", "0"),
            ("--interval", "inf"),
            ("--max-interval", "-1"),
            ("--quality", "32"),
            ("--frame-quality", "0"),
            ("--max-edge", "-1"),
            ("--phash-threshold", "65"),
        ):
            with self.subTest(flag=name, value=value), self.assertRaises(ValueError):
                self.compile("-t", self.text, name, value)
            self.assertFalse(self.output.exists())

    def test_offsets_reject_invalid_or_reversed_values(self):
        for values in (["-1"], ["00:60"], ["1:99:00"], ["nan"], ["1.5"], ["10", "5"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                digest.resolve_offsets([Path("v.mp4")] * len(values), values)

    def test_empty_text_is_an_error(self):
        self.text.write_text(" \n")
        with self.assertRaisesRegex(ValueError, "no spoken text"):
            self.compile("-t", self.text)
        self.assertFalse(self.output.exists())

    def test_existing_package_is_unchanged_on_rerun(self):
        self.compile("-t", self.text)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        self.text.write_text("[00:00] Different source")
        with self.assertRaisesRegex(ValueError, "already compiled"):
            self.compile("-t", self.text)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.output.iterdir()}
        )

    def test_real_pdf_ingestion_then_transcript(self):
        pdf = self.root / "slides.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(pdf)
        self.compile("--slides", pdf, "--title", "Materials first")
        self.assertFalse((self.output / "transcript.md").exists())
        self.assertIn("Materials first", (self.output / "README.md").read_text())
        self.assertEqual(
            pdf.read_bytes(), (self.output / "materials" / "slides.pdf").read_bytes()
        )
        self.compile("-t", self.text)
        self.assertTrue((self.output / "materials" / "slide-links.md").exists())
        self.assertIn("materials/slides.pdf", (self.output / "README.md").read_text())

    def test_bad_slide_does_not_publish_transcript(self):
        pdf = self.root / "invalid.pdf"
        pdf.write_bytes(b"invalid PDF")
        with self.assertRaisesRegex(ValueError, "Could not read slide PDF"):
            self.compile("-t", self.text, "--slides", pdf)
        self.assertFalse(self.output.exists())


class TestVideoModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="digest_video_")
        cls.root = Path(cls.temp.name)
        cls.silent = cls.root / "silent.mp4"
        cls.audible = cls.root / "audible.mp4"
        cls.delayed = cls.root / "delayed.mp4"
        for dest, audio in (
            (cls.silent, False),
            (cls.audible, True),
            (cls.delayed, True),
        ):
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=3:size=160x120:rate=10",
            ]
            if audio:
                if dest == cls.delayed:
                    cmd += ["-itsoffset", "1"]
                cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
            cmd += ["-pix_fmt", "yuv420p", "-t", "3", str(dest)]
            subprocess.run(cmd, check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.temp_output = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp_output.cleanup)
        self.output = Path(self.temp_output.name) / "package"
        self.text = Path(self.temp_output.name) / "text.txt"
        self.text.write_text("[00:00] Supplied transcript wins.")
        self.model = Mock()
        self.model.transcribe.side_effect = lambda *a, **kw: (
            iter([SimpleNamespace(start=0.4, text=" Recognized speech.")]),
            SimpleNamespace(language="en"),
        )
        self.backend = SimpleNamespace(WhisperModel=Mock(return_value=self.model))

    def compile(self, *values):
        digest.compile_package(arguments(*values, "-o", self.output))

    def test_mp4_with_supplied_text_ignores_audio(self):
        with patch.object(
            transcript,
            "transcribe_videos",
            side_effect=AssertionError("ASR must not run"),
        ):
            self.compile(self.audible, "-t", self.text, "--no-crop", "--interval", "1")
        self.assertIn(
            "Supplied transcript wins.", (self.output / "transcript.md").read_text()
        )
        self.assertTrue((self.output / "frames" / "00-00-00.jpg").exists())
        self.assertNotIn("faster-whisper", (self.output / "README.md").read_text())

    def test_silent_video_cannot_be_transcribed(self):
        with self.assertRaisesRegex(ValueError, "No audio stream"):
            self.compile(self.silent, "--transcribe")
        self.assertFalse(self.output.exists())

    def test_corrupt_video_is_rejected(self):
        bad = Path(self.temp_output.name) / "bad.mp4"
        bad.write_bytes(b"invalid")
        with self.assertRaisesRegex(ValueError, "Could not read video"):
            self.compile(bad, "--transcribe")
        self.assertFalse(self.output.exists())

    def test_audio_mode_aligns_recognized_text_and_frames(self):
        with installed_backend(self.backend):
            self.compile(
                self.audible,
                "--transcribe",
                "--offsets",
                "10",
                "--model",
                "tiny",
                "--language",
                "en",
                "--no-crop",
                "--interval",
                "1",
            )
        self.assertIn("## 00:00:10", (self.output / "transcript.md").read_text())
        self.assertIn("Recognized speech.", (self.output / "transcript.md").read_text())
        self.assertTrue((self.output / "frames" / "00-00-10.jpg").exists())
        self.assertIn("Local faster-whisper", (self.output / "README.md").read_text())
        self.assertFalse(list(self.output.rglob("*.wav")))
        self.assertFalse((self.output / "frames" / "raw").exists())

    def test_multiple_recordings_preserve_gaps_and_load_model_once(self):
        with installed_backend(self.backend):
            self.compile(
                self.audible,
                self.audible,
                "--transcribe",
                "--offsets",
                "0",
                "10",
                "--no-frames",
            )
        content = (self.output / "transcript.md").read_text()
        self.assertIn("## 00:00:00", content)
        self.assertIn("## 00:00:10", content)
        self.assertEqual(self.backend.WhisperModel.call_count, 1)
        self.assertEqual(self.model.transcribe.call_count, 2)
        self.assertFalse((self.output / "frames").exists())
        self.assertIn("starts at `00:00:10`", (self.output / "README.md").read_text())

    def test_audio_overlap_requires_correction(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.compile(
                self.audible, self.audible, "--transcribe", "--offsets", "0", "1"
            )
        self.assertFalse(self.output.exists())

    def test_missing_backend_has_install_instruction(self):
        with (
            installed_backend(None),
            self.assertRaisesRegex(RuntimeError, "uv sync --extra audio"),
        ):
            self.compile(self.audible, "--transcribe")
        self.assertFalse(self.output.exists())

    def test_no_speech_does_not_publish_package(self):
        self.model.transcribe.side_effect = lambda *a, **kw: (iter([]), None)
        with (
            installed_backend(self.backend),
            self.assertRaisesRegex(ValueError, "No speech"),
        ):
            self.compile(self.audible, "--transcribe")
        self.assertFalse(self.output.exists())

    def test_delayed_audio_keeps_leading_silence(self):
        def inspect_audio(filename, **kwargs):
            with wave.open(filename) as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertGreaterEqual(audio.getnframes(), 47000)
                # 0.5 seconds precedes the delayed track's first sample.
                self.assertEqual(set(audio.readframes(8000)), {0})
            return iter([SimpleNamespace(start=1.0, text="Speech after silence")]), None

        self.model.transcribe.side_effect = inspect_audio
        with installed_backend(self.backend):
            self.compile(self.delayed, "--transcribe", "--no-frames")
        self.assertIn("## 00:00:01", (self.output / "transcript.md").read_text())

    def test_processing_failure_preserves_ingested_materials(self):
        materials = self.output / "materials"
        materials.mkdir(parents=True)
        pdf = materials / "slides.pdf"
        pdf.write_bytes(b"existing material")
        (self.output / "README.md").write_text("Slides only")
        with (
            patch.object(
                transcript, "transcribe_videos", side_effect=RuntimeError("ASR failed")
            ),
            self.assertRaisesRegex(RuntimeError, "ASR failed"),
        ):
            self.compile(self.audible, "--transcribe")
        self.assertEqual(pdf.read_bytes(), b"existing material")
        self.assertEqual((self.output / "README.md").read_text(), "Slides only")
        self.assertFalse((self.output / "transcript.md").exists())


if __name__ == "__main__":
    unittest.main()
