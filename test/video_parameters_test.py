import unittest

from tools.video_parameters import resolve_frame_count, resolve_video_dimensions


class VideoParametersTest(unittest.TestCase):
    def test_explicit_size_wins_and_aligns_to_32(self):
        self.assertEqual(
            resolve_video_dimensions(640, 640, size="1280x720"),
            (1280, 736),
        )

    def test_resolution_dimensions_are_used_before_aspect_ratio(self):
        self.assertEqual(
            resolve_video_dimensions(640, 640, resolution="512x384", aspect_ratio="16:9"),
            (512, 384),
        )

    def test_aspect_ratio_uses_short_edge(self):
        self.assertEqual(
            resolve_video_dimensions(640, 640, resolution="720p", aspect_ratio="16:9"),
            (1280, 736),
        )
        self.assertEqual(
            resolve_video_dimensions(640, 640, resolution="720p", aspect_ratio="9:16"),
            (736, 1280),
        )

    def test_aspect_ratio_without_resolution_keeps_short_edge(self):
        self.assertEqual(
            resolve_video_dimensions(640, 640, aspect_ratio="9:16"),
            (640, 1152),
        )

    def test_megapixels_resolution_scales_with_aspect_ratio(self):
        self.assertEqual(
            resolve_video_dimensions(640, 640, resolution="0.9", aspect_ratio="16:9"),
            (1280, 736),
        )

    def test_widget_dimensions_are_kept_without_video_specs(self):
        self.assertEqual(resolve_video_dimensions(640, 640), (640, 640))
        self.assertEqual(resolve_video_dimensions(1024, 576), (1024, 576))

    def test_frame_count_includes_last_frame(self):
        self.assertEqual(resolve_frame_count(5, 24), 121)
        self.assertEqual(resolve_frame_count(5.5, 24), 133)
        self.assertEqual(resolve_frame_count(0, 24), 1)

    def test_frame_count_falls_back_to_defaults(self):
        self.assertEqual(resolve_frame_count("bad", None), 81)


if __name__ == "__main__":
    unittest.main()
