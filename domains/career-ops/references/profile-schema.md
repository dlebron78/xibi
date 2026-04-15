# Profile Schema Reference

Documents every field in `config/profile.yml`.

## Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Full name as it appears on resume |
| `email` | string | Yes | Professional email |
| `phone` | string | No | Formatted phone number |
| `linkedin_url` | string | No | Full LinkedIn profile URL |
| `portfolio_url` | string | No | Personal site, GitHub, or portfolio |
| `location` | string | Yes | City, State or Country |
| `work_preference` | enum | Yes | `remote`, `hybrid`, `onsite`, `flexible` |
| `visa_status` | string | No | e.g., "US Citizen", "H-1B", "OPT" |

## `current` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | Yes | Current or most recent job title |
| `company` | string | No | Current or most recent employer |
| `years_experience` | int | Yes | Total years of relevant professional experience |

## `target` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `primary_role` | string | Yes | Target job title (e.g., "Senior Software Engineer") |
| `secondary_roles` | list[string] | No | Additional role titles to consider |
| `industries` | list[string] | No | Target industries; empty = open to all |
| `seniority` | enum | Yes | `entry`, `mid`, `senior`, `lead`, `director`, `vp`, `c-suite` |
| `exclude_keywords` | list[string] | No | Posting keywords that disqualify a role |

## `skills`

Type: `list[string]`
Required: Yes (at minimum 5 skills)
Description: Top technical and domain skills, strongest first. Used for keyword matching in triage and evaluate.

## `credentials`

Type: `list[object]`
Optional. Each object:
- `name`: certification/license name
- `issuer`: issuing organization
- `year`: year obtained (integer)

## `narrative` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `headline` | string | Yes | 1-line professional identity |
| `story` | string | Yes | 3–5 sentences: background, transition (if any), current focus |
| `superpowers` | string | Yes | 2–3 distinctive strengths |

## `work_history`

Type: `list[object]`
Required for tailor-resume and outreach. Each object:
- `title`: job title
- `company`: employer name
- `dates`: date range (e.g., "2022–present" or "Jan 2020–Mar 2022")
- `highlights`: list[string] of bullet-point achievements (verb-first, quantified)

## `education`

Type: `list[object]`
Optional. Each object:
- `degree`: degree name
- `institution`: school name
- `year`: graduation year (integer)
- `notes`: optional string (coursework, honors, GPA)

## `compensation` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `target` | string | Yes | Total comp target (e.g., "$200K") |
| `minimum` | string | Yes | Walk-away floor (e.g., "$160K base") |
| `currency` | string | No | Default "USD" |
| `equity_preference` | string | No | "preferred", "indifferent", or "avoid" |

## `proof_points`

Type: `list[string]`
Optional but strongly recommended. Quantified achievements used in Block E (Positioning) and to seed story bank. Format: result-first, with metric.

## `portfolio`

Type: `list[object]`
Optional. Each object:
- `title`: project name
- `url`: link
- `description`: 1-sentence description

## `persona` Object

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `recent_graduate` | bool | false | < 2 years experience; softens seniority penalties |
| `career_changer` | bool | false | Switching industries; softens domain fit penalties |
| `career_returner` | bool | false | Returning from gap; softens recency penalties |
| `international` | bool | false | Credentials from outside target market |
| `gap_explanation` | string | "" | Brief explanation of any employment gap |
