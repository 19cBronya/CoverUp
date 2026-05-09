from coverup.sampling import decide_window, uniform_points


def test_uniform_points_count_and_range() -> None:
    points = uniform_points(0.0, 60.0, 12)
    assert len(points) == 12
    assert all(0.0 <= p <= 60.0 for p in points)
    assert points[0] < points[-1]


def test_decide_window_normal_then_tail_then_loop() -> None:
    first = decide_window(duration=130.0, minute_index=0)
    assert first.window_start == 0.0
    assert first.window_end == 60.0
    assert first.next_minute_index == 1

    second = decide_window(duration=130.0, minute_index=1)
    assert second.window_start == 60.0
    assert second.window_end == 120.0
    assert second.next_minute_index == 2

    tail = decide_window(duration=130.0, minute_index=2)
    assert tail.window_start == 120.0
    assert tail.window_end == 130.0
    assert tail.is_tail_window is True
    assert tail.next_minute_index == 0


def test_decide_window_short_video_loops() -> None:
    first = decide_window(duration=20.0, minute_index=0)
    assert first.window_start == 0.0
    assert first.window_end == 20.0
    assert first.next_minute_index == 0
