from types import SimpleNamespace


class FakeBucket:
    def __init__(self, existing):
        self._existing = existing

    def get_blob(self, name):
        return object() if name in self._existing else None


class FakeStorage:
    def __init__(self, existing=()):
        self._existing = set(existing)

    def bucket(self, name):
        return FakeBucket(self._existing)


def doc(name, uri):
    return SimpleNamespace(name=name, content=SimpleNamespace(uri=uri))


class FakeDocuments:
    def __init__(self, docs):
        self._docs = docs
        self.deleted = []

    def list_documents(self, parent):
        return list(self._docs)

    def delete_document(self, name):
        self.deleted.append(name)


BRANCH = "projects/p/locations/global/collections/default_collection/dataStores/ds/branches/default_branch"


def test_deleted_object_is_removed_from_the_index(ingestion):
    docs = FakeDocuments([doc("d/1", "gs://b/documents/raw/a.pdf"), doc("d/2", "gs://b/documents/raw/other.pdf")])
    removed = ingestion.remove_from_index(
        "b", "documents/raw/a.pdf", storage_client=FakeStorage(), document_client=docs, branch=BRANCH
    )
    assert removed == 1
    assert docs.deleted == ["d/1"]


def test_overwritten_object_keeps_its_index_entry(ingestion):
    docs = FakeDocuments([doc("d/1", "gs://b/github/raw/o/r/README.md")])
    removed = ingestion.remove_from_index(
        "b",
        "github/raw/o/r/README.md",
        storage_client=FakeStorage(existing={"github/raw/o/r/README.md"}),
        document_client=docs,
        branch=BRANCH,
    )
    assert removed == 0 and docs.deleted == []


def test_unsupported_objects_are_ignored(ingestion):
    docs = FakeDocuments([doc("d/1", "gs://b/manifest/run.log")])
    assert ingestion.remove_from_index("b", "manifest/run.log", storage_client=FakeStorage(), document_client=docs, branch=BRANCH) == 0
    assert docs.deleted == []


def test_missing_index_entry_is_not_an_error(ingestion):
    docs = FakeDocuments([])
    assert ingestion.remove_from_index("b", "documents/raw/gone.txt", storage_client=FakeStorage(), document_client=docs, branch=BRANCH) == 0


def test_find_document_names_matches_exact_uri_only(ingestion):
    docs = FakeDocuments([doc("d/1", "gs://b/x.md"), doc("d/2", "gs://b/x.md.bak"), doc("d/3", "gs://b/x.md")])
    assert ingestion.find_document_names(docs, BRANCH, "gs://b/x.md") == ["d/1", "d/3"]
