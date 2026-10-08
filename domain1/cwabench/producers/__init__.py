"""The producer pipeline S11 measures (domain-1-plan.md, section 9): a synthetic source corpus, a deterministic
chunker, variant producers (stub compressors, or an OpenAI-compatible summarizer behind a content-addressed cache),
deterministic fidelity checks and R-18's variant rules, and the freeze into a snapshot the assemblers read.

Everything here runs before freeze. Assembly never sees a model: it sees only the variants this pipeline froze.
"""
