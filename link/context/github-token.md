# GitHub Token — Setup & Verification

> This file tells Link exactly what GitHub token it needs, how to confirm the
> token works, and how to diagnose common errors.

---

## What Token Link Needs

Link must have a GitHub API token that is allowed to **trigger workflow runs**
on the `leeheggan-droid/openclaw-control` repository.

| Token type       | Required permission                                              |
|------------------|------------------------------------------------------------------|
| Fine-grained PAT | **Actions: Read and Write** on `leeheggan-droid/openclaw-control` |
| Classic PAT      | **`workflow`** scope                                             |

> Classic PAT with only `repo` scope is **not enough** — it cannot trigger
> `workflow_dispatch` events.

---

## How to Verify the Token Works

### Step 1 — check the token is valid and can see the repo

```
GET https://api.github.com/repos/leeheggan-droid/openclaw-control/actions/workflows
Authorization: Bearer <GITHUB_TOKEN>
```

Expected response:
```json
{
  "total_count": 1,
  "workflows": [
    {
      "id": ...,
      "name": "Link Control",
      "path": ".github/workflows/link.yml",
      "state": "active"
    }
  ]
}
```

If `state` is `"active"` and the name is `"Link Control"`, the token can see
the workflow and the workflow file exists on the default branch.

### Step 2 — test a safe dispatch (`status-all`)

```
POST https://api.github.com/repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>
Content-Type: application/json

{
  "ref": "main",
  "inputs": {
    "action": "status-all"
  }
}
```

Expected response: **`HTTP 204 No Content`** with an empty body.

Then immediately check:
```
GET https://api.github.com/repos/leeheggan-droid/openclaw-control/actions/runs?event=workflow_dispatch&per_page=1
```
You should see a run appear with `status: "queued"` or `"in_progress"`.

---

## Error Reference

| HTTP status | Likely cause                                                              | Fix                                                             |
|-------------|---------------------------------------------------------------------------|------------------------------------------------------------------|
| `401`       | Token is missing, expired, or malformed                                   | Re-generate the PAT; check it is set correctly in Link's config |
| `403`       | Token exists but lacks `Actions: write` / `workflow` scope                | Re-generate with the correct permission (see table above)       |
| `404` on dispatch | Wrong workflow filename in URL — must be exactly `link.yml`        | Check `.github/workflows/` directory; confirm file is on `main` |
| `404` on GET workflows | Token cannot read the repo (e.g. private repo, wrong account) | Confirm the token belongs to `leeheggan-droid` or a collaborator with read access |
| `422`       | `ref` does not exist, or `inputs` contains a key the workflow doesn't define | Use `"ref": "main"` and only send `action`, `service`, `tail_lines` |

---

## Where Link Stores the Token

Link (Vercel app at `www.leeheggan.tech`) stores its GitHub token as an
environment variable in the Vercel project settings.  The variable name is
defined in the Link repo — check the Link repo's README or `.env.example` for
the exact variable name.  **Do not hard-code the token anywhere in this repo.**

---

*Last updated: 2026-05-04*
