from math import tanh
from pathlib import Path
from re import sub
from tempfile import TemporaryDirectory
from time import sleep
from typing import Optional

from git import PushInfo, Repo
from pyzotero.zotero import Zotero
from tqdm.auto import tqdm
from unidecode import unidecode
from yaml import safe_load

CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"
REQUEST_NUMBER = 0


def normalize(name: str) -> str:
    name = unidecode(name)
    name = name.lower()
    name = name.replace("\n", "-")
    name = name.replace("\"", "-")
    name = name.replace("'", "-")
    name = name.replace(".", "-")
    name = name.replace(":", "-")
    name = name.replace(";", "-")
    name = name.replace("!", "-")
    name = name.replace("?", "-")
    name = name.replace("(", "-")
    name = name.replace(")", "-")
    name = name.replace(" ", "-")
    name = sub(r"-+", "-", name)
    name = name.removesuffix("-")
    return name


def download_pdf(
        zotero: Zotero,
        git_export_path: Path,
        item: dict,
        old_path: Optional[Path],
        git_repository: Repo,
        git_repository_path: Path,
) -> Optional[Path]:
    global REQUEST_NUMBER

    if "attachment" not in item["links"]:
        return None
    if item["links"]["attachment"]["attachmentType"] != "application/pdf":
        return None

    pdf_url = item["links"]["attachment"]["href"]
    pdf_id = pdf_url.split("/")[-1]

    first_author_last_names = [
        creator["lastName"] if "lastName" in creator else creator["name"]
        for creator in item["data"]["creators"]
        if creator["creatorType"] == "author"
    ]
    first_author_last_names.append("noauthor")
    first_author_last_name = normalize(first_author_last_names[0])
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
    title = normalize(item["data"]["title"])
    file_name = f"{first_author_last_name}{year}-{title}.pdf"
    new_path = git_export_path / file_name

    if old_path is not None and old_path.exists():
        # File already exists. Just rename it if needed.
        if old_path != new_path:
            if new_path.exists():
                git_repository.index.remove([
                    str(new_path.relative_to(git_repository_path)),
                ], working_tree=True)
            git_repository.index.move([
                str(old_path.relative_to(git_repository_path)),
                str(new_path.relative_to(git_repository_path)),
            ])
    else:
        with new_path.open("wb") as file:
            file.write(
                zotero.file(pdf_id)
            )
            sleep(10 * tanh(REQUEST_NUMBER / 10))
            REQUEST_NUMBER += 1
        git_repository.index.add([
            str(new_path.relative_to(git_repository_path))
        ])

    return new_path


def sync(
        zotero_api_key: str,
        zotero_user_id: str,
        zotero_collection_id: str,
        git_repository_url: str,
        git_name: str,
        git_email: str,
        export_path: str,
        commit_message: str,
) -> None:
    global REQUEST_NUMBER
    REQUEST_NUMBER = 0

    with TemporaryDirectory() as temp_dir:
        git_repository_path = Path(temp_dir)
        git_repository = Repo.clone_from(
            git_repository_url,
            git_repository_path,
            multi_options=["--depth", "1"]
        )
        with git_repository.config_writer() as git_config:
            git_config.set_value("user", "name", git_name)
            git_config.set_value("user", "email", git_email)

        # Make directories.
        git_export_path = git_repository_path / export_path
        git_export_path.mkdir(exist_ok=True)
        other_dir_path = git_export_path / "other"
        other_dir_path.mkdir(exist_ok=True)

        # Setup API.
        zotero = Zotero(zotero_user_id, "user", zotero_api_key)

        # Get collection items.
        top_items = zotero.collection_items_top(zotero_collection_id)
        top_items_id = [
            (item, item["links"]["attachment"]["href"].split("/")[-1])
            for item in top_items
            if "attachment" in item["links"] and
               item["links"]["attachment"]["attachmentType"] ==
               "application/pdf"
        ]

        # Find old files.
        paths_old: set[Path] = {
            path
            for path in git_export_path.iterdir()
            if path.is_file() and path.suffix == ".pdf"
        }

        # Find old lock file.
        lockfile_path = git_export_path / ".zotero"
        lockfile_path.touch()
        with lockfile_path.open("rt") as file:
            lock_old: dict[str, Path] = {}
            for line in file.readlines():
                item_id, filename = line.strip().split()
                lock_old[item_id] = git_export_path / filename

        # Download missing files.
        id_paths_new: dict[str, Path] = {
            item_id: download_pdf(
                zotero,
                git_export_path,
                item,
                lock_old.get(item_id, None),
                git_repository,
                git_repository_path,
            )
            for item, item_id in tqdm(
                top_items_id,
                desc="Download PDFs",
                unit="file",
            )
        }
        id_paths_new = {
            item_id: path
            for item_id, path in id_paths_new.items()
            if path is not None
        }

        # Move other literature to subfolder
        paths_renamed: set[Path] = {
            path
            for item_id, path in lock_old.items()
            if item_id in id_paths_new.keys()
        }
        paths_other = paths_old - paths_renamed
        for old_path in paths_other:
            new_path = other_dir_path / old_path.name
            if old_path != new_path:
                if new_path.exists():
                    num = 1
                    while new_path.exists():
                        new_path = (
                                other_dir_path /
                                f"{old_path.stem}.{num}{old_path.suffix}"
                        )
                        num += 1
                    git_repository.index.remove([
                        str(new_path.relative_to(git_repository_path)),
                    ], working_tree=True)
                git_repository.index.move([
                    str(old_path.relative_to(git_repository_path)),
                    str(new_path.relative_to(git_repository_path)),
                ])

        with lockfile_path.open("wt") as file:
            for item_id, old_path in id_paths_new.items():
                file_name = old_path.name
                file.write(f"{item_id} {file_name}\n")
        git_repository.index.add([
            str(lockfile_path.relative_to(git_repository_path))
        ])

        if not git_repository.is_dirty():
            print("Nothing changed.")
            return

        git_repository.index.commit(commit_message)
        git_push_info: PushInfo = git_repository.remotes.origin.push()[0]
        print(git_push_info.flags)
        assert git_push_info.flags == PushInfo.FAST_FORWARD


def main() -> None:
    with CONFIG_FILE.open("r") as file:
        config = safe_load(file)
    zotero_api_key = config["zoteroApiKey"]
    zotero_user_id = config["zoteroUserId"]
    zotero_collection_id = config["zoteroCollectionId"]
    git_repository_url = config["gitRepositoryUrl"]
    git_name = config["gitName"]
    git_email = config["gitEmail"]
    export_path = config["exportPath"]
    commit_message = config["commitMessage"]
    sync(
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
