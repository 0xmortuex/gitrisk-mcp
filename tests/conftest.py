import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from gitrepo import Builder, build_acme, build_release_fixture  # noqa: E402

from gitrisk_mcp.git import Repo, Sandbox  # noqa: E402


@pytest.fixture(scope="session")
def acme_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp("acme_root") / "acme")


@pytest.fixture(scope="session")
def acme(acme_dir: str) -> Builder:
    return build_acme(acme_dir)


@pytest.fixture(scope="session")
def acme_repo(acme: Builder) -> Repo:
    return Sandbox([os.path.dirname(acme.path)]).open(acme.path)


@pytest.fixture(scope="session")
def rel_repo(tmp_path_factory: pytest.TempPathFactory) -> Repo:
    root = tmp_path_factory.mktemp("rel_root")
    b = build_release_fixture(str(root / "rel"))
    return Sandbox([str(root)]).open(b.path)


@pytest.fixture
def acme_copy(acme: Builder, tmp_path) -> str:  # type: ignore[no-untyped-def]
    dst = tmp_path / "copy"
    shutil.copytree(acme.path, dst)
    return str(dst)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
