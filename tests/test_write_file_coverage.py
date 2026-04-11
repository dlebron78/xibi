import json
import os

from xibi.skills.sample.filesystem.tools import write_file


def test_write_file_no_path():
    res = write_file.run({})
    assert res["status"] == "error"
    assert "Missing filepath" in res["message"]

def test_write_file_no_content_no_handle():
    res = write_file.run({"filepath": "t.txt"})
    assert res["status"] == "error"
    assert "Provide exactly one" in res["message"]

def test_write_file_both_content_and_handle():
    res = write_file.run({"filepath": "t.txt", "content": "a", "handle": "b"})
    assert res["status"] == "error"
    assert "Provide exactly one" in res["message"]

def test_write_file_handle_list(tmp_path):
    f = str(tmp_path / "l.json")
    res = write_file.run({"filepath": f, "handle": [1, 2]})
    assert res["status"] == "success"
    with open(f) as fp:
        assert json.load(fp) == [1, 2]

def test_write_file_handle_other(tmp_path):
    f = str(tmp_path / "o.txt")
    res = write_file.run({"filepath": f, "handle": 123})
    assert res["status"] == "success"
    with open(f) as fp:
        assert fp.read() == "123"

def test_write_file_relative_path(tmp_path):
    workdir = str(tmp_path)
    res = write_file.run({"filepath": "rel.txt", "content": "hello", "_workdir": workdir})
    assert res["status"] == "success"
    assert os.path.exists(os.path.join(workdir, "rel.txt"))

def test_write_file_open_error(tmp_path):
    # directory instead of file
    d = str(tmp_path / "dir")
    os.mkdir(d)
    res = write_file.run({"filepath": d, "content": "fail"})
    assert res["status"] == "error"
