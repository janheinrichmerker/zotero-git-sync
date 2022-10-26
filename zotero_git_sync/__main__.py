from pathlib import Path
from re import sub
from tempfile import TemporaryDirectory
from typing import Optional

from git import PushInfo, Repo
from pyzotero.zotero import Zotero
from tqdm.auto import tqdm
from unidecode import unidecode
from yaml import safe_load

CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"


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
        item: dict
) -> Optional[Path]:
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
        year = year[:2]
    title = normalize(item["data"]["title"])
    filename = f"{first_author_last_name}{year}-{title}.pdf"

    zotero.dump(pdf_id, filename=filename, path=git_export_path)

    return git_export_path / filename


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

        git_export_path = git_repository_path / export_path
        git_export_path.mkdir(exist_ok=True)

        zotero = Zotero(zotero_user_id, "user", zotero_api_key)

        top_items = zotero.collection_items_top(zotero_collection_id)
        top_items = tqdm(
            top_items,
            desc="Download PDFs",
            unit="file",
        )
        paths_old = {
            path
            for path in git_export_path.iterdir()
            if path.is_file() and path.suffix == ".pdf"
        }
        paths_new = {
            download_pdf(zotero, git_export_path, item)
            for item in top_items
        }
        paths_old_not_overwritten = paths_old - paths_new

        other_dir_path = git_export_path / "other"
        other_dir_path.mkdir(exist_ok=True)
        path: Path
        # Move other literature to subfolder
        for path in paths_old_not_overwritten:
            path.replace(other_dir_path / path.name)

        if not git_repository.is_dirty(untracked_files=True):
            return

        git_repository.index.add([
            git_export_path.relative_to(git_repository_path)
        ])
        git_repository.index.commit(commit_message)
        git_push_info: PushInfo = git_repository.remotes.origin.push()[0]
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
