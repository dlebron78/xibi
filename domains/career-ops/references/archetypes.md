# Archetypes

15 industry archetypes with detection logic and scoring weight adjustments. The evaluate skill runs archetype detection first, then applies the matching archetype's weight overrides to the scoring rubric.

## Detection Algorithm

1. Extract: job title, requirements list, description text.
2. Apply keyword weights: title keywords × 3, requirements keywords × 2, description keywords × 1.
3. Calculate weighted keyword overlap for each archetype.
4. Select the archetype with the highest score. If top two are within 15% of each other, flag as "dual-archetype" and blend their weight overrides (50/50 split).
5. If no archetype scores above threshold (< 3 keyword hits), use "General" (default weights from scoring-rubric.md, no overrides).

---

## Archetypes

### 1. Technology

**Detection keywords:** software engineer, developer, architect, devops, sre, platform, backend, frontend, fullstack, mobile, ios, android, data engineer, ml engineer, ai engineer, llm, python, javascript, typescript, rust, go, java, kubernetes, aws, gcp, azure, cloud, infrastructure, security engineer

**Scoring weight overrides:**
- Role-skill alignment: 28%
- Technical requirements coverage: 18%
- Seniority match: 12%
- Industry/domain fit: 8%
- Compensation alignment: 12%
- Cultural alignment: 7%
- Growth/trajectory fit: 7%
- Location/remote: 5%
- Company stage: 2%
- Career narrative: 1%

**Credential requirements:** None typically required. Security clearance if noted.

**AI Sub-archetype Detection** (runs only when primary archetype = Technology):
After selecting Technology as primary archetype, run a second detection pass against these 6 sub-archetypes:

- **FDE (Field/Demo Engineer):** customer-facing, demo, proof of concept, solutions engineer, pre-sales, technical account — prioritize client-facing proof points and delivery speed in Block E.
- **Solution Architect:** architecture, design patterns, cloud-native, microservices, enterprise architecture, technical strategy — prioritize systems thinking and cross-domain range.
- **AI Product Manager:** product manager AI, LLM product, ai features, model evaluation, prompt engineering pm — prioritize business impact over code, balance technical and product proof points.
- **LLMOps/ML Platform:** mlops, llmops, model deployment, inference, fine-tuning, evaluation pipeline, vector store, embedding, rag, retrieval — prioritize systems, scale, and evaluation rigor.
- **Agentic Systems:** agentic, multi-agent, orchestration, tool use, human-in-the-loop, hitl, workflow automation, autonomous — prioritize orchestration depth, HITL design, error handling in Block F questions.
- **AI Transformation:** digital transformation, ai strategy, change management, ai adoption, enterprise ai — balance technical credibility with organizational influence.

If no sub-archetype scores above threshold, use base Technology framing.

---

### 2. Finance

**Detection keywords:** fintech, banking, financial services, investment, trading, quant, hedge fund, private equity, venture capital, compliance, risk, audit, actuarial, wealth management, payments, lending, underwriting

**Scoring weight overrides:**
- Role-skill alignment: 22%
- Industry/domain fit: 18%  ← elevated; finance domain experience matters a lot
- Technical requirements coverage: 14%
- Seniority match: 14%
- Compensation alignment: 12%
- Cultural alignment: 6%
- Growth/trajectory fit: 6%
- Location/remote: 5%
- Company stage: 2%
- Career narrative: 1%

**Credential requirements:** CFA, CPA, Series 7/63/65, FRM — noted when present in posting.

---

### 3. Healthcare

**Detection keywords:** healthcare, hospital, clinical, ehr, epic, cerner, hipaa, fda, pharmaceutical, biotech, medical device, nursing, physician, patient, radiology, telehealth, health system

**Scoring weight overrides:**
- Role-skill alignment: 22%
- Industry/domain fit: 20%  ← healthcare domain experience is near-mandatory
- Technical requirements coverage: 13%
- Seniority match: 13%
- Compensation alignment: 9%
- Cultural alignment: 8%
- Growth/trajectory fit: 7%
- Location/remote: 5%
- Company stage: 2%
- Career narrative: 1%

**Credential requirements:** Clinical licensure if clinical role. HIPAA compliance awareness expected.

---

### 4. Legal

**Detection keywords:** law firm, legal, attorney, counsel, paralegal, compliance, contract, litigation, intellectual property, patent, regulatory, bar exam, j.d., llm degree, legal tech

**Scoring weight overrides:**
- Role-skill alignment: 20%
- Industry/domain fit: 22%  ← legal domain is highly specialized
- Technical requirements coverage: 12%
- Seniority match: 15%
- Credentials: replaces Career narrative at 5%
- Compensation alignment: 10%
- Cultural alignment: 7%
- Growth/trajectory fit: 6%
- Location/remote: 3%

**Credential requirements:** J.D. and bar admission if practicing attorney role. Always noted.

---

### 5. Creative / Marketing

**Detection keywords:** marketing, brand, creative director, designer, ux, ui, content, copywriter, social media, seo, growth hacker, demand generation, campaign, art director, motion graphics, video production, creative

**Scoring weight overrides:**
- Role-skill alignment: 25%
- Portfolio/proof points: replaces Technical requirements at 15%
- Industry/domain fit: 8%
- Seniority match: 12%
- Cultural alignment: 12%  ← culture fit matters more for creative roles
- Compensation alignment: 9%
- Growth/trajectory fit: 8%
- Location/remote: 6%
- Company stage: 4%
- Career narrative: 1%

---

### 6. Operations

**Detection keywords:** operations, supply chain, logistics, warehouse, manufacturing, process improvement, lean, six sigma, plant manager, facilities, procurement, vendor management, ops manager

**Scoring weight overrides:**
- Role-skill alignment: 24%
- Seniority match: 16%
- Industry/domain fit: 14%
- Technical requirements coverage: 12%
- Compensation alignment: 10%
- Cultural alignment: 7%
- Growth/trajectory fit: 8%
- Location/remote: 6%
- Company stage: 2%
- Career narrative: 1%

---

### 7. Sales / Business Development

**Detection keywords:** sales, account executive, account manager, business development, bd, revenue, quota, cold outreach, pipeline, crm, salesforce, enterprise sales, smb, channel partner, solution sales

**Scoring weight overrides:**
- Role-skill alignment: 22%
- Seniority match: 14%
- Industry/domain fit: 12%
- Compensation alignment: 16%  ← comp structure (OTE vs base) is critical
- Cultural alignment: 10%
- Technical requirements coverage: 8%
- Growth/trajectory fit: 8%
- Location/remote: 5%
- Company stage: 3%
- Career narrative: 2%

---

### 8. Education

**Detection keywords:** teacher, professor, curriculum, instructional design, edtech, learning and development, l&d, training, coach, tutor, academic, school, university, k-12

**Scoring weight overrides:**
- Role-skill alignment: 22%
- Industry/domain fit: 16%
- Seniority match: 12%
- Cultural alignment: 14%
- Technical requirements coverage: 10%
- Compensation alignment: 8%
- Growth/trajectory fit: 8%
- Location/remote: 7%
- Company stage: 2%
- Career narrative: 1%

---

### 9. Executive

**Detection keywords:** ceo, cto, cfo, coo, cpo, vp engineering, vp product, chief of staff, general manager, director, svp, evp, c-suite, board, executive leadership

**Scoring weight overrides:**
- Role-skill alignment: 18%
- Seniority match: 20%  ← exact level match matters most at executive level
- Industry/domain fit: 16%
- Compensation alignment: 14%
- Cultural alignment: 12%
- Growth/trajectory fit: 10%
- Technical requirements coverage: 4%
- Location/remote: 3%
- Company stage: 2%
- Career narrative: 1%

---

### 10. Trades

**Detection keywords:** electrician, plumber, hvac, carpenter, welder, machinist, technician, field service, installation, maintenance, skilled trades, apprentice, journeyman

**Scoring weight overrides:**
- Role-skill alignment: 30%
- Technical requirements coverage: 20%
- Credentials: replaces Cultural alignment at 12%
- Industry/domain fit: 10%
- Seniority match: 10%
- Location/remote: 10%  ← trades are inherently location-bound
- Compensation alignment: 5%
- Company stage: 2%
- Growth/trajectory fit: 1%

**Credential requirements:** Licenses (electrician, HVAC, etc.) are often mandatory — always noted.

---

### 11. Customer Success

**Detection keywords:** customer success, csm, customer success manager, account management, churn, nrr, renewal, customer health, onboarding, adoption, client success, customer experience

**Scoring weight overrides:**
- Role-skill alignment: 24%
- Industry/domain fit: 12%
- Seniority match: 14%
- Cultural alignment: 12%
- Compensation alignment: 10%
- Technical requirements coverage: 10%
- Growth/trajectory fit: 8%
- Location/remote: 6%
- Company stage: 3%
- Career narrative: 1%

---

### 12. People / HR

**Detection keywords:** hr, human resources, recruiting, talent acquisition, people ops, hrbp, compensation and benefits, dei, organizational development, talent management, payroll, employee relations

**Scoring weight overrides:**
- Role-skill alignment: 22%
- Industry/domain fit: 10%
- Seniority match: 14%
- Cultural alignment: 16%  ← cultural fit is especially important for People roles
- Compensation alignment: 10%
- Technical requirements coverage: 8%
- Growth/trajectory fit: 8%
- Location/remote: 6%
- Company stage: 4%
- Career narrative: 2%

---

### 13. Government / Nonprofit

**Detection keywords:** government, federal, state agency, nonprofit, ngo, mission-driven, policy, public sector, grant, foundation, advocacy, community, social impact, 501c3

**Scoring weight overrides:**
- Role-skill alignment: 22%
- Industry/domain fit: 16%
- Cultural alignment: 16%  ← mission alignment is a strong signal
- Seniority match: 12%
- Compensation alignment: 6%  ← comp is often non-negotiable in public sector
- Technical requirements coverage: 10%
- Growth/trajectory fit: 8%
- Location/remote: 5%
- Company stage: 3%
- Career narrative: 2%

**Special notes:** Government/academic postings often remain open 60–90 days — this is normal, not a legitimacy signal. Clearance requirements are hard gates.

---

### 14. Scientific / R&D

**Detection keywords:** research scientist, r&d, laboratory, data scientist, computational biology, bioinformatics, materials science, chemistry, physics, postdoc, research engineer, publications required

**Scoring weight overrides:**
- Role-skill alignment: 24%
- Technical requirements coverage: 18%
- Industry/domain fit: 14%
- Credentials: adds 8% weight (PhD, publications, domain certifications)
- Seniority match: 10%
- Cultural alignment: 6%
- Compensation alignment: 8%
- Growth/trajectory fit: 7%
- Location/remote: 4%
- Career narrative: 1%

**Credential requirements:** PhD often required or strongly preferred — always call out explicitly.

---

### 15. Non-Software Engineering

**Detection keywords:** mechanical engineer, civil engineer, structural engineer, aerospace engineer, electrical engineer, chemical engineer, environmental engineer, pe license, cad, solidworks, autocad, fea, cfd

**Scoring weight overrides:**
- Role-skill alignment: 26%
- Technical requirements coverage: 20%
- Industry/domain fit: 14%
- Credentials: adds 8% (PE license, domain certifications)
- Seniority match: 12%
- Compensation alignment: 8%
- Cultural alignment: 5%
- Growth/trajectory fit: 5%
- Location/remote: 4%
- Career narrative: 1%

**Credential requirements:** PE license is sometimes mandatory — always noted.
