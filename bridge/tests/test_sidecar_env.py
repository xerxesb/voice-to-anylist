"""The environment handed to the Node sidecar.

The bridge reads .env with python-dotenv; Node's own --env-file reads it
differently.  They disagree about an unquoted '#': Node treats it as the start
of a comment and truncates, python-dotenv keeps it.  A password containing '#'
reached the sidecar two characters short, and AnyList answered with a bare 401
that named no cause.

One parser reads the file and hands the sidecar a plain environment, so the
class of bug goes rather than this instance of it.
"""

from voice_to_anylist.sidecar import build_env


def write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_a_hash_in_a_value_survives(tmp_path):
    """Node's --env-file would stop at the '#'. This is the regression."""
    env_file = write(tmp_path, "ANYLIST_PASSWORD=ab#cd\n")

    assert build_env({}, env_file)["ANYLIST_PASSWORD"] == "ab#cd"


def test_a_whole_line_comment_is_still_a_comment(tmp_path):
    env_file = write(tmp_path, "# ANYLIST_PASSWORD=nope\nANYLIST_EMAIL=a@b.com\n")

    env = build_env({}, env_file)

    assert env["ANYLIST_EMAIL"] == "a@b.com"
    assert "ANYLIST_PASSWORD" not in env


def test_quoted_values_lose_their_quotes(tmp_path):
    env_file = write(tmp_path, 'ANYLIST_PASSWORD="ab#cd"\n')

    assert build_env({}, env_file)["ANYLIST_PASSWORD"] == "ab#cd"


def test_the_real_environment_wins(tmp_path):
    """The plist carries deployment facts; .env carries credentials."""
    env_file = write(tmp_path, "ANYLIST_API_PORT=9999\n")

    env = build_env({"ANYLIST_API_PORT": "3000"}, env_file)

    assert env["ANYLIST_API_PORT"] == "3000"


def test_env_file_fills_in_what_the_environment_lacks(tmp_path):
    env_file = write(tmp_path, "ANYLIST_EMAIL=a@b.com\n")

    env = build_env({"PATH": "/usr/bin"}, env_file)

    assert env["ANYLIST_EMAIL"] == "a@b.com"
    assert env["PATH"] == "/usr/bin"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    """A host that has not been configured yet must still start."""
    env = build_env({"PATH": "/usr/bin"}, tmp_path / "absent.env")

    assert env == {"PATH": "/usr/bin"}


def test_trailing_whitespace_and_blank_lines_are_tolerated(tmp_path):
    env_file = write(tmp_path, "\nANYLIST_EMAIL=a@b.com\n\n")

    assert build_env({}, env_file)["ANYLIST_EMAIL"] == "a@b.com"
