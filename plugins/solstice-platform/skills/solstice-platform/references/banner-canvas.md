# Banner and social canvas format

Banner and social assets are **multi-document**: one complete, standalone HTML
document per dimension, concatenated. Email is a single document; this page does
not apply to it.

```html
<!DOCTYPE html>
<html><head><meta name="ad.size" content="width=300,height=250"></head>…</html>

<!DOCTYPE html>
<html><head><meta name="ad.size" content="width=728,height=90"></head>…</html>
```

Rules, all of them load-bearing:

- **Every document opens with `<!DOCTYPE html>`.** This is the delimiter the
  platform splits on. A document that reaches storage without one is read as a
  continuation of the document before it, so the canvas silently loses a size —
  and `solstice_commit_operation_version` rejects the upload.
- **Join with a blank line.** One document ends `</html>`, the next begins its
  declaration.
- **Stamp each document with its size**, via `data-ad-size="300x250"` on the
  banner root or `<meta name="ad.size" content="width=300,height=250">`. Unstamped
  documents cannot be matched to a dimension.
- **Never wrap the sizes in an outer document.** A gallery page that embeds each
  banner in an `<iframe srcdoc="…">` is not a canvas; the platform cannot split
  it, and assets in that shape are not editable.

## Reading a canvas

`solstice_operation_html` returns a presigned URL for the whole canvas, not per
dimension. Download it and split on the `<!DOCTYPE html>` boundaries yourself.
Expect N documents for an N-dimension asset.

## Editing a canvas

Whatever the task — an ISI swap, a copy change, a job-code update — it applies
**per document**, and every document you did not intend to change must come back
byte-identical. Two failure modes to avoid:

- Editing only the first document. A six-dimension banner needs six edits.
- Round-tripping the canvas through an HTML parser and re-serializing. Element
  serialization does not carry the DOCTYPE node, so a parse-and-reserialize
  silently drops declarations and merges dimensions. Edit the text.
