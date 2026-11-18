import os

from app.config import load_dotenv


def test_dotenv_parsing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('﻿HR_T_A=auto   # comment\nHR_T_B="quoted # value"\nHR_T_C=\nHR_T_D=   # only comment\n'
                   "# HR_T_E=commented-out\nHR_T_F='single'\n", encoding="utf-8")
    for k in ("HR_T_A", "HR_T_B", "HR_T_C", "HR_T_D", "HR_T_E", "HR_T_F"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HR_T_F", "real-env-wins")
    load_dotenv(env)
    assert os.environ["HR_T_A"] == "auto"          # BOM + inline comment handled
    assert os.environ["HR_T_B"] == "quoted # value"
    assert "HR_T_C" not in os.environ and "HR_T_D" not in os.environ and "HR_T_E" not in os.environ
    assert os.environ["HR_T_F"] == "real-env-wins"
    for k in ("HR_T_A", "HR_T_B"):
        monkeypatch.delenv(k)
