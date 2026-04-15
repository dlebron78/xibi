# ATS Optimization Rules

Rules applied by the tailor-resume skill to produce ATS-compatible resumes.

## Core Principles

1. **Mirror the JD language** — Use the exact phrasing from the job description for key skills and requirements. ATS systems match keywords literally. "Machine learning" and "ML" are different tokens.

2. **Front-load keywords** — Keywords in the first 1/3 of the resume score more heavily in most ATS systems. Role title, summary, and top skills section are highest-value real estate.

3. **Verb-first bullets** — Each bullet begins with a strong action verb in past tense (Led, Built, Reduced, Drove, Designed, Shipped, Scaled, Implemented, Owned, Launched, Optimized).

4. **Quantify everything possible** — Numbers pass through ATS and signal concreteness to humans. "Reduced latency" < "Reduced p99 latency from 800ms to 120ms (85% reduction)". If no exact number is known, use a range or relative improvement.

## Formatting Constraints (ATS Parsing Rules)

These formatting patterns break most ATS parsers and must be avoided:

| Avoid | Use Instead |
|-------|-------------|
| Multi-column layouts | Single-column, left-aligned |
| Tables for skills/experience | Plain text lists |
| Text in graphics or images | Plain text only |
| Headers in text boxes | Standard markdown headers |
| Fancy fonts or symbols | Standard ASCII characters |
| PDFs with scanned text | Text-selectable output |
| "Creative" section names | Standard names: Summary, Experience, Education, Skills, Projects |

## Section Order (Standard)

1. **Contact Information** — Name, email, phone, LinkedIn, location, portfolio
2. **Professional Summary** — 3–5 sentences tailored to the specific role
3. **Skills** — Keyword-dense list, organized by category if > 8 skills
4. **Experience** — Reverse chronological, company + title + dates + 3–6 bullets each
5. **Education** — Degree + institution + year. GPA only if > 3.7 and within 5 years.
6. **Projects / Portfolio** — Only if directly relevant to the role. Link if applicable.
7. **Certifications / Credentials** — Required for regulated industries.

## Keyword Injection Strategy

1. Extract all required skills from the JD — these are mandatory keywords.
2. Extract preferred/bonus skills — inject where they truthfully apply.
3. Check the Summary section: it should contain at least 3 JD-specific keywords.
4. Check each Experience entry: bullets should reference the tools/methods listed in the JD.
5. Skills section should include every keyword from the JD that the candidate legitimately has.

## Honesty Constraint (NON-NEGOTIABLE)

The resume must reflect only real experience from the candidate's profile.

- Do NOT fabricate projects, roles, or credentials.
- Do NOT claim skills not present in profile.skills or demonstrated in work_history.
- Do NOT adjust dates or inflate titles.
- If a required JD skill is not in the profile: note it as a gap in the output's `gaps` field. Do NOT silently omit it or invent coverage.

This constraint is non-negotiable. A fabricated resume destroys the candidate's credibility permanently.

## Tailoring Instructions Per Role Type

**Technical roles:** Lead with technical skills, emphasize scale and complexity of systems.

**Leadership roles:** Front-load team size, scope, and business outcomes. De-emphasize individual contributor bullets.

**Product roles:** Lead with business impact (revenue, user growth, retention) over technical implementation details.

**Research roles:** List publications, patents, or open-source contributions prominently if present.
