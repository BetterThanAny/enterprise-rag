# Evaluation data and provenance

## Controlled regression sets

- `retrieval.jsonl`: 200 synthetic, deliberately separable policy queries used as a deterministic
  regression gate for SQL retrieval modes and latency.
- `generation.jsonl`: 20 answerable and 20 abstention cases used for citation/abstention regression.

These sets are useful for repeatability and failure localization. Their lexical Recall@5 of 1.0
and large relative hybrid/dense deltas are not production-representative quality claims.

## Public human-labeled evidence set

M6 evaluates the unchanged SciFact dev release from the Allen Institute for AI:

- Source: <https://github.com/allenai/scifact>
- Archive: `https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz`
- SHA-256: `11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be`
- Claims/evidence annotations: CC BY 4.0
- Abstract corpus: ODC-By 1.0
- Evaluated subset: all 5,183 abstracts and the 188 dev claims with non-empty human evidence

The third-party archive is downloaded to the user cache and is not redistributed in this
repository. Reports preserve source, licenses, checksum, model version, sizes, quality metrics,
latency, and limitations. SciFact validates scientific evidence retrieval, not enterprise-policy
production performance.
