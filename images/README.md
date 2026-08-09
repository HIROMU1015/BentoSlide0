# Local image library

- `user/`: images supplied by the user.
- `extracted/`: figures cropped or extracted from registered source documents by Work/GPT.
- `generated/`: explanatory images created by Work/GPT; never use these as source evidence.

These folders are the visible local library. Their image contents are ignored by Git to prevent accidental publication. Agents register selected images transactionally into the hidden `deck/assets/` tree, record origin/provenance and `contentDigest`, and use only the registered copy during conversion and approval.
