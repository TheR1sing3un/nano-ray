"""Example 04: Word Count (MapReduce pattern with task chains).

Demonstrates:
- Map phase: parallel text processing tasks
- Reduce phase: task that depends on all map results (DAG)
- Classic MapReduce pattern expressed with nano-ray primitives
"""

import nano_ray as nanoray


@nanoray.remote
def word_count_map(text: str) -> dict[str, int]:
    """Map phase: count words in a text chunk."""
    counts: dict[str, int] = {}
    for word in text.lower().split():
        word = word.strip(".,!?;:")
        if word:
            counts[word] = counts.get(word, 0) + 1
    return counts


@nanoray.remote
def word_count_reduce(count_list: list[dict[str, int]]) -> dict[str, int]:
    """Reduce phase: merge word counts from all mappers."""
    merged: dict[str, int] = {}
    for counts in count_list:
        for word, count in counts.items():
            merged[word] = merged.get(word, 0) + count
    return merged


if __name__ == "__main__":
    nanoray.init(num_workers=4)

    # Sample text corpus split into chunks
    texts = [
        "the quick brown fox jumps over the lazy dog",
        "the fox is quick and the dog is lazy",
        "the brown dog chased the quick fox",
        "a lazy fox and a quick dog met in the park",
    ]

    # Map phase: parallel word counting
    map_refs = [word_count_map.remote(text) for text in texts]

    # Reduce phase: merge all counts (depends on all map tasks)
    result_ref = word_count_reduce.remote(map_refs)

    # Get final result
    word_counts = nanoray.get(result_ref)

    # Sort by count (descending) and display
    sorted_counts = sorted(word_counts.items(), key=lambda x: -x[1])
    print("Word counts:")
    for word, count in sorted_counts[:10]:
        print(f"  {word:>10s}: {count}")

    # Verification
    assert word_counts["the"] == 7
    assert word_counts["fox"] == 4
    assert word_counts["quick"] == 4
    print("\nAll checks passed!")

    nanoray.shutdown()
