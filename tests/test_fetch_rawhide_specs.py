from unittest.mock import MagicMock, patch

from scripts.fetch_rawhide_specs import clone_package


@patch('subprocess.run')
def test_clone_package_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert clone_package("pkg", "dest")
    mock_run.assert_called_once()

@patch('subprocess.run')
def test_clone_package_failure(mock_run):
    mock_run.return_value = MagicMock(returncode=1)
    assert not clone_package("pkg", "dest")
