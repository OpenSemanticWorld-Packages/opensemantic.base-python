"""Wrap a CSV that lives on the web in a Dataset.

A ``Dataset`` carries its rows either inline in ``data`` or by reference,
through a ``Distribution`` that names the file holding them. This builds the
second form over a CSV nobody here controls, then reads the rows back into the
typed model with one call.

``AddressRow`` gives the columns their types. That is what makes the dataset
more than a file: ``materialize_data()`` knows what each column means, so the
rows come back as models rather than strings in a frame.

Run from the package root::

    python examples/dataset_from_remote_csv.py
"""

from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

from opensemantic.base import Dataset, DatasetControllerMixin, Distribution
from opensemantic.core import Label, LocalFile, RemoteFile

CSV_URL = (
    "https://raw.githubusercontent.com/w3c/csvw/refs/heads/gh-pages/"
    "examples/dm-example-1/addresses.csv"
)


class AddressRow(BaseModel):
    """One line of the CSV. Field names match the column headers."""

    ID: Optional[int] = None
    city: Optional[str] = None
    state: Optional[str] = None


class AddressDataset(Dataset):
    """A dataset whose rows are addresses."""

    data: Optional[List[AddressRow]] = None


class AddressDatasetController(DatasetControllerMixin, AddressDataset):
    """The dataset plus the conversions: rows to a frame, to and from a file."""


def main() -> None:
    # 1. The dataset refers to the file instead of holding its content. The
    #    file entity is a RemoteFile, so reading it needs no local copy.
    dataset = AddressDatasetController(
        label=[Label(text="W3C CSVW example addresses")],
        distributions=[
            Distribution(
                file=RemoteFile(
                    label=[Label(text="addresses.csv")], download_url=CSV_URL
                ),
                media_type="text/csv",
            )
        ],
    )
    print(f"{len(dataset.distributions)} distribution, data: {dataset.data}")

    # 2. Read the file and build the typed rows from it.
    rows = dataset.materialize_data()
    for row in rows:
        print(
            f"  {type(row).__name__}(ID={row.ID!r}, city={row.city!r}, "
            f"state={row.state!r})"
        )

    # 3. The rows are now inline, and the frame form carries the same columns.
    print(f"\n{dataset.to_df()}")

    # 4. Back to CSV. The second row names each column's unit and is empty
    #    where a column has none, so a table of plain values writes and reads
    #    like any other CSV.
    print(f"\n{dataset.serialize_data('csv').decode('utf-8')}")

    # 5. The whole loop: write the rows to a local file, let a Distribution
    #    describe it, drop the inline copy, and read them back.
    target = Path(__file__).parent / "addresses.roundtrip.csv"
    dataset.externalize_data(
        LocalFile(label=[Label(text=target.name)], local_path=str(target)),
        fmt="csv",
    )
    print(f"data after externalize: {dataset.data}")

    restored = dataset.materialize_data(dataset.distributions[-1])
    print(f"read back from {target.name}: {restored}")
    target.unlink()


if __name__ == "__main__":
    main()
