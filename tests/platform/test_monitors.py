import pytest

pytestmark = pytest.mark.platform


@pytest.fixture
def platform():
    from magent.platform import get_platform

    return get_platform()


class TestListMonitors:
    def test_at_least_one_monitor(self, platform):
        monitors = platform.list_monitors()
        assert len(monitors) >= 1

    def test_monitor_has_positive_dimensions(self, platform):
        monitors = platform.list_monitors()
        for m in monitors:
            assert m.w > 0
            assert m.h > 0

    def test_monitor_has_scale_factor(self, platform):
        monitors = platform.list_monitors()
        for m in monitors:
            assert m.scale_factor >= 1.0

    def test_exactly_one_primary(self, platform):
        monitors = platform.list_monitors()
        primaries = [m for m in monitors if m.is_primary]
        assert len(primaries) == 1
