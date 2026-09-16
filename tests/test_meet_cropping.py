"""Meet cropping contracts, independent of a locally installed OCR engine."""

import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import digest
import frames


class TestMeetCropping(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def frame(self, name="frame_000001.jpg", box=(40, 170, 700, 500), seed=0):
        image = Image.new("RGB", (1000, 650), (16, 16, 16))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 999, 90), fill=(50, 50, 50))
        draw.rectangle((350, 610, 650, 640), fill=(70, 70, 70))
        if box:
            left, top, right, bottom = box
            draw.rectangle((left, top, right - 1, bottom - 1), fill=(235, 235, 235))
            draw.text((left + 20, top + 20), "Static shared screen", fill="black")
        rng = np.random.default_rng(seed)
        for top in (140, 360):
            webcam = Image.fromarray(
                rng.integers(40, 255, (200, 240, 3), dtype=np.uint8)
            )
            image.paste(webcam, (730, top))
        path = self.root / name
        image.save(path, quality=95)
        return path

    def ocr(
        self,
        address="meet.google.com/abc-defg-hij",
        presenter="(Presenting)",
        confidence=95,
        address_y=30,
        presenter_y=80,
        presenter_x=780,
    ):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        rows = [
            f"5\t1\t1\t1\t1\t1\t350\t{address_y}\t200\t12\t{confidence}\t{address}\n",
            f"5\t1\t2\t1\t1\t1\t{presenter_x}\t{presenter_y}\t100\t12\t{confidence}\t{presenter}\n",
        ]
        return patch.object(
            frames.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, (header + "".join(rows)).encode(), b""
            ),
        )

    def assert_box_preserves_screen(self, detected, expected):
        self.assertIsNotNone(detected)
        for index, (actual, edge) in enumerate(zip(detected, expected)):
            self.assertLessEqual(abs(actual - edge), 5)
            if index < 2:
                self.assertLessEqual(actual, edge)
            else:
                self.assertGreaterEqual(actual, edge)
        self.assertLess(
            detected[2], 730, "Participant tiles must stay outside the crop"
        )

    def test_static_share_is_cropped_despite_changing_webcams(self):
        with self.ocr():
            for seed in (1, 2, 3):
                with self.subTest(seed=seed):
                    box = frames.detect_meet_content_box(
                        self.frame(seed=seed), "tesseract"
                    )
                    self.assert_box_preserves_screen(box, (40, 170, 700, 500))

    def test_camera_off_tiles_do_not_change_the_crop(self):
        path = self.frame()
        with Image.open(path) as source:
            image = source.copy()
        draw = ImageDraw.Draw(image)
        draw.rectangle((730, 140, 970, 560), fill=(16, 16, 16))
        for y in (160, 290, 420):
            for x in (730, 815, 900):
                draw.rectangle((x, y, x + 65, y + 105), fill=(90, 45, 120))
        image.save(path, quality=95)
        with self.ocr():
            self.assert_box_preserves_screen(
                frames.detect_meet_content_box(path, "tesseract"), (40, 170, 700, 500)
            )

    def test_non_meet_video_is_not_cropped_even_with_same_geometry(self):
        with self.ocr(address="web.whatsapp.com"):
            self.assertIsNone(frames.detect_meet_content_box(self.frame(), "tesseract"))

    def test_inactive_meet_tab_is_not_enough(self):
        with self.ocr(address="Meet - abc-defg-hij"):
            self.assertIsNone(frames.detect_meet_content_box(self.frame(), "tesseract"))

    def test_domain_in_other_host_is_not_meet(self):
        with self.ocr(address="notmeet.google.com/abc-defg-hij"):
            self.assertIsNone(frames.detect_meet_content_box(self.frame(), "tesseract"))

    def test_meet_without_presentation_is_not_cropped(self):
        with self.ocr(presenter="Participants"):
            self.assertIsNone(frames.detect_meet_content_box(self.frame(), "tesseract"))

    def test_portuguese_presentation_indicator(self):
        with self.ocr(presenter="(Apresentando)"):
            self.assertIsNotNone(
                frames.detect_meet_content_box(self.frame(), "tesseract")
            )

    def test_uncertain_or_misplaced_header_is_not_enough(self):
        for options in (
            {"confidence": 40},
            {"address_y": 90},
            {"presenter_y": 20},
            {"presenter_x": 200},
        ):
            with self.subTest(options=options), self.ocr(**options):
                self.assertIsNone(
                    frames.detect_meet_content_box(self.frame(), "tesseract")
                )

    def test_gallery_without_dominant_screen_is_not_cropped(self):
        with self.ocr():
            self.assertIsNone(
                frames.detect_meet_content_box(self.frame(box=None), "tesseract")
            )

    def test_missing_top_or_bottom_gutter_is_ambiguous(self):
        for box in ((40, 100, 700, 500), (40, 170, 700, 590)):
            with self.subTest(box=box), self.ocr():
                self.assertIsNone(
                    frames.detect_meet_content_box(self.frame(box=box), "tesseract")
                )

    def test_ocr_failure_keeps_full_frame_and_reports_reason(self):
        failures = (subprocess.TimeoutExpired("tesseract", 15), OSError("unavailable"))
        for failure in failures:
            with (
                self.subTest(failure=failure),
                patch.object(frames.subprocess, "run", side_effect=failure),
            ):
                errors = io.StringIO()
                with redirect_stderr(errors):
                    self.assertIsNone(
                        frames.detect_meet_content_box(self.frame(), "tesseract")
                    )
                self.assertIn("keeping full frame", errors.getvalue())
        with patch.object(
            frames.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, b"", b"missing language data"
            ),
        ):
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertIsNone(
                    frames.detect_meet_content_box(self.frame(), "tesseract")
                )
            self.assertIn("missing language data", errors.getvalue())

    def test_each_frame_rechecks_layout_and_active_app(self):
        first = self.frame()
        second = self.frame("frame_000002.jpg", box=(40, 145, 670, 500))
        third = self.frame("frame_000003.jpg")
        raw = self.root / "raw"
        raw.mkdir()
        for source in (first, second, third):
            shutil.copyfile(source, raw / source.name)
        # The third frame switched to another app with similar geometry.
        with (
            patch.object(frames.shutil, "which", return_value="tesseract"),
            patch.object(
                frames, "_meet_presenting_header", side_effect=[True, True, False]
            ),
        ):
            summaries = frames.crop_frames(raw, self.root / "output", max_edge=0)
        self.assertEqual(summaries[0], frames.CropSummary(3, 2))
        sizes = []
        for path in sorted((self.root / "output").iterdir()):
            with Image.open(path) as image:
                sizes.append(image.size)
        self.assertNotEqual(sizes[0], sizes[1])
        self.assertEqual(sizes[2], (1000, 650))

    def test_missing_ocr_preserves_frames_with_diagnostic(self):
        raw = self.root / "raw"
        raw.mkdir()
        shutil.copyfile(self.frame(), raw / "frame_000001.jpg")
        output = io.StringIO()
        with (
            patch.object(frames.shutil, "which", return_value=None),
            redirect_stdout(output),
        ):
            summaries = frames.crop_frames(raw, self.root / "output", max_edge=0)
        self.assertEqual(summaries[0], frames.CropSummary(1, 0))
        self.assertIn("Tesseract is unavailable", output.getvalue())
        with Image.open(self.root / "output" / "frame_000001.jpg") as image:
            self.assertEqual(image.size, (1000, 650))

    def test_no_crop_and_manual_box_bypass_ocr(self):
        raw = self.root / "raw"
        raw.mkdir()
        shutil.copyfile(self.frame(), raw / "frame_000001.jpg")
        with patch.object(
            frames,
            "detect_meet_content_box",
            side_effect=AssertionError("OCR must not run"),
        ):
            for name, options, expected in (
                ("none", {"detect": False}, (1000, 650)),
                ("manual", {"box": (40, 170, 700, 500)}, (660, 330)),
            ):
                with self.subTest(name=name):
                    frames.crop_frames(raw, self.root / name, max_edge=0, **options)
                    with Image.open(self.root / name / "frame_000001.jpg") as image:
                        self.assertEqual(image.size, expected)

    def test_package_describes_mixed_cropping_honestly(self):
        description = digest.describe_crop({0: frames.CropSummary(3, 2)})
        self.assertIn("2/3 frames cropped", description)
        self.assertIn("all other frames retained whole", description)
        manual = digest.describe_crop(
            {0: frames.CropSummary(1, 1, (40, 170, 700, 500))}
        )
        self.assertIn("40,170,700,500", manual)
        self.assertIn("manual override", manual)
