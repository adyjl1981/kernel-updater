import io
import json
import unittest
from unittest import mock
import test_kernel_manager as existing

gui = existing.gui

class ReleaseChoicesTests(unittest.TestCase):
    def test_sorted_deduplicated_final_releases_include_base(self):
        data = {'releases': [{'version': v} for v in ['7.3', '7.3.1', '6.18.60', '7.3', '7.4-rc1', 'next-20261101']]}
        with mock.patch.object(gui.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(data).encode())):
            self.assertEqual(gui.kernel_release_choices(), ['7.3.1', '7.3', '6.18.60'])

    def test_refresh_does_not_replace_selected_version(self):
        app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        app.kernel_version_var = mock.Mock()
        app.kernel_version_combo = mock.Mock()
        app._read_async = lambda key, work, done: done(['7.3.1', '7.3'])
        app.refresh_kernel_releases()
        app.kernel_version_var.set.assert_not_called()
        app.kernel_version_combo.configure.assert_called_once_with(values=('Latest stable', '7.3.1', '7.3'))
