# Prompts

One `.md` file per prompt. Prompts are files, not inline strings, so eval results can be tied to a prompt version.

Each file starts with a version header:

```
---
name: rank_cluster
version: 1
model: fast
---
```

Bump `version` on any change to the prompt text, then rerun the matching eval.
