from pathlib import Path
from re import sub
from tempfile import TemporaryDirectory
from typing import Optional

from git import Repo
from ratelimit import rate_limited, sleep_and_retry
from requests import Session
from requests.adapters import HTTPAdapter
from tqdm.auto import tqdm
from unidecode import unidecode
from urllib3 import Retry
from yaml import safe_load

_CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"


def _read_lock(lock_path: Path, export_path: Path) -> dict[str, Path]:
    lock_path.touch()
    with lock_path.open("rt") as file:
        lock_lines = (
            line.strip().split()
            for line in file.readlines()
        )
        return {
            item_id.strip(): export_path / file_name.strip()
            for item_id, file_name in lock_lines
        }


def _write_lock(lock_path: Path, lock: dict[str, Path]) -> None:
    lock_items = sorted(lock.items(), key=lambda item: item[0])
    with lock_path.open("wt") as file:
        for item_id, path in lock_items:
            file_name = path.name
            file.write(f"{item_id} {file_name}\n")


def _item_id(item: dict) -> str:
    return item["links"]["attachment"]["href"].split("/")[-1]


def _item_has_pdf_attachment(item: dict) -> bool:
    if "attachment" not in item["links"]:
        return False
    return item["links"]["attachment"]["attachmentType"] == "application/pdf"


def _normalize_name(name: str) -> str:
    name = unidecode(name)
    name = name.lower()
    name = name.replace("\n", "-")
    name = name.replace("\"", "-")
    name = name.replace("'", "-")
    name = name.replace(".", "-")
    name = name.replace(":", "-")
    name = name.replace(";", "-")
    name = name.replace(",", "-")
    name = name.replace("!", "-")
    name = name.replace("?", "-")
    name = name.replace("(", "-")
    name = name.replace(")", "-")
    name = name.replace("[", "-")
    name = name.replace("]", "-")
    name = name.replace("+", "-")
    name = name.replace("&", "-")
    name = name.replace("_", "-")
    name = name.replace(" ", "-")
    name = sub(r"-+", "-", name)
    name = name.removeprefix("-")
    name = name.removesuffix("-")
    return name


def _item_path(item: dict, export_path: Path) -> Path:
    first_author_last_names = [
        creator["lastName"] if "lastName" in creator else creator["name"]
        for creator in item["data"]["creators"]
        if creator["creatorType"] == "author"
    ]
    first_author_last_names.append("noauthor")
    first_author_last_name = _normalize_name(first_author_last_names[0])
    date: str
    if "parsedDate" in item["meta"]:
        date = item["meta"]["parsedDate"]
    else:
        date = ""
    year: str
    if "/" in date:
        year = date.split("/")[-1]
    elif "-" in date:
        year = date.split("-")[0]
    else:
        year = date
    if len(year) > 2:
        year = year[-2:]
    title = _normalize_name(item["data"]["title"])
    file_name = f"{first_author_last_name}{year}-{title}.pdf"
    return export_path / file_name


def _move_to_subdir(path: Path, subdir_path: Path) -> None:
    new_path = subdir_path / path.name
    if new_path.exists():
        increment = 1
        while new_path.exists():
            new_path = subdir_path / f"{path.stem}.{increment}{path.suffix}"
            increment += 1
    path.rename(new_path)


@sleep_and_retry
@rate_limited(calls=5, period=15)
def _download_pdf(
        session: Session,
        zotero_api_key: str,
        zotero_user_id: str,
        item: dict,
        item_path: Path,
) -> None:
    pdf_url = item["links"]["attachment"]["href"]
    pdf_id = pdf_url.split("/")[-1]

    response = session.get(
        url=f"https://api.zotero.org/"
            f"users/{zotero_user_id}/"
            f"items/{pdf_id.upper()}/file",
        headers={
            "Zotero-API-Version": "3",
            "Authorization": f"Bearer {zotero_api_key}",
        },
    )
    with item_path.open("wb") as file:
        file.write(response.content)


def _get_items(
        session: Session,
        zotero_api_key: str,
        zotero_user_id: str,
        zotero_collection_id: str,
) -> dict[str, dict]:
    url = (
        f"https://api.zotero.org/users/{zotero_user_id}/"
        f"collections/{zotero_collection_id}/items/top"
    )
    headers = {
        "Zotero-API-Version": "3",
        "Authorization": f"Bearer {zotero_api_key}",
    }
    total_items_headers = session.get(url, headers=headers).headers
    total_items = int(total_items_headers["Total-Results"])

    progress = tqdm(
        total=total_items,
        desc="Get collection items",
        unit="item",
    )
    limit = 10
    all_items = {}
    for start in range(0, total_items, limit):
        items_response = session.get(
            f"{url}?start={start}&limit={limit}",
            headers=headers,
        )
        items_response_json = items_response.json()
        progress.update(len(items_response_json))
        all_items.update({
            _item_id(item): item
            for item in items_response.json()
            if _item_has_pdf_attachment(item)
        })

    return all_items


def _sync(
        zotero_api_key: str,
        zotero_user_id: str,
        zotero_collection_id: str,
        git_repository_url: str,
        git_name: str,
        git_email: str,
        export_path: str,
        commit_message: str,
) -> None:
    session = Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))

    with TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        repo = Repo.clone_from(
            git_repository_url,
            repo_path,
            depth=1,
        )
        with repo.config_writer() as git_config:
            git_config.set_value("user", "name", git_name)
            git_config.set_value("user", "email", git_email)

        # Make directories.
        export_path = repo_path / export_path
        export_path.mkdir(exist_ok=True)
        subdir_path = export_path / "other"
        subdir_path.mkdir(exist_ok=True)

        # Find and read lock file.
        lock_path = export_path / ".zotero"
        lock = _read_lock(lock_path, export_path)

        # Find old files.
        existing_paths: set[Path] = {
            path
            for path in export_path.iterdir()
            if path.is_file() and path.suffix == ".pdf"
        }

        # Get collection items with PDFs.
        items = _get_items(
            session,
            zotero_api_key,
            zotero_user_id,
            zotero_collection_id,
        )

        # Compute mappings from old to new paths.
        item_paths: list[tuple[str, Optional[Path], Path]] = [
            (
                item_id,
                lock.get(item_id, None),
                _item_path(item, export_path)
            )
            for item_id, item in items.items()
        ]

        # Move or rename existing files.
        move_paths = {
            old_path: new_path
            for item_id, old_path, new_path in item_paths
            if old_path is not None
        }
        for path in existing_paths:
            if path in move_paths:
                # Still in use, rename.
                path.rename(move_paths[path])
            else:
                # No corresponding item, move to subdir.
                _move_to_subdir(path, subdir_path)

        # Download files.
        download_items = [
            (item_id, new_path)
            for item_id, old_path, new_path in item_paths
            if old_path is None
        ]
        download_items = tqdm(
            download_items,
            desc="Download PDFs",
            unit="file",
        )
        for item_id, new_path in download_items:
            _download_pdf(
                session,
                zotero_api_key,
                zotero_user_id,
                items[item_id],
                new_path
            )

        # Write lock file
        lock = {item_id: new_path for item_id, _, new_path in item_paths}
        _write_lock(lock_path, lock)

        if not repo.is_dirty(untracked_files=True):
            # Nothing has changed.
            return
        print("Add and commit files to repository.")
        repo.git.add(".")
        print(repo.git.commit(message=commit_message))
        repo.git.pull()
        print("Push changes.")
        repo.git.push()
        # Push twice as sometimes if LFS needs to long,
        # the Git push seems to be forgotten.
        repo.git.push()
        status = repo.git.status().strip()
        if not all(
                status_line in status
                for status_line in {
                    "branch is up to date",
                    "nothing to commit",
                    "working tree clean"
                }
        ):
            raise RuntimeError(f"Push unsuccessful: {status}")


def main() -> None:
    with _CONFIG_FILE.open("r") as file:
        config = safe_load(file)
    zotero_api_key = config["zoteroApiKey"]
    zotero_user_id = config["zoteroUserId"]
    zotero_collection_id = config["zoteroCollectionId"]
    git_repository_url = config["gitRepositoryUrl"]
    git_name = config["gitName"]
    git_email = config["gitEmail"]
    export_path = config["exportPath"]
    commit_message = config["commitMessage"]
    _sync(
        zotero_api_key,
        zotero_user_id,
        zotero_collection_id,
        git_repository_url,
        git_name,
        git_email,
        export_path,
        commit_message,
    )


if __name__ == "__main__":
    main()
