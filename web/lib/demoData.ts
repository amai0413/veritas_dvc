// Three scripted scenarios — hardcoded sources AND votes so the product tells a
// story with no live users and no live fetching. Each lands on a different status.

import type { ClaimInput, Vote } from "./types";

// votes(trueCount, falseCount) — build a vote tier quickly.
function votes(trueCount: number, falseCount: number): Vote[] {
  return [
    ...Array.from({ length: trueCount }, () => ({ verdict: "true" as const })),
    ...Array.from({ length: falseCount }, () => ({ verdict: "false" as const })),
  ];
}

export interface DemoScenario {
  id: string;
  label: string;
  blurb: string;
  claim: ClaimInput;
}

export const DEMO_SCENARIOS: DemoScenario[] = [
  // A. AGREEMENT — obvious fake. High-reliability sources contradict and the
  //    crowd votes false. Everything clusters on the contradict side. -> false
  {
    id: "agreement",
    label: "Agreement",
    blurb: "Obvious fake — AI and crowd both say false.",
    claim: {
      text: "Drinking bleach cures the common cold.",
      aiVerdict: {
        score: 3,
        reasoning:
          "Ingesting bleach is corrosive and toxic, and no evidence shows it treats any infection. Health authorities explicitly warn against it.",
      },
      sources: [
        {
          title: "CDC: Dangers of Ingesting Disinfectants",
          url: "https://www.cdc.gov/",
          type: "gov",
          date: "2023",
          stance: -0.97,
          reliability: 0.97,
          relevance: 0.95,
          summary:
            "The CDC documents severe harm from ingesting household bleach and lists no therapeutic use.",
          whyItMatters:
            "A primary government health authority directly contradicting the claim.",
        },
        {
          title: "WHO: Cleaning products are not treatments",
          url: "https://www.who.int/",
          type: "gov",
          date: "2022",
          stance: -0.95,
          reliability: 0.96,
          relevance: 0.9,
          summary:
            "WHO guidance states disinfectants must never be swallowed or injected to treat illness.",
          whyItMatters: "Global health body corroborating the CDC.",
        },
        {
          title: "Journal of Medical Toxicology: Hypochlorite ingestion",
          url: "https://link.springer.com/journal/13181",
          type: "academic",
          date: "2021",
          stance: -0.9,
          reliability: 0.92,
          relevance: 0.8,
          summary:
            "Peer-reviewed case series describing esophageal injury from bleach ingestion; no antiviral benefit.",
          whyItMatters: "Peer-reviewed clinical evidence of harm.",
        },
        {
          title: "Reuters Health: No, bleach doesn't cure colds",
          url: "https://www.reuters.com/",
          type: "news",
          date: "2020",
          stance: -0.85,
          reliability: 0.8,
          relevance: 0.75,
          summary:
            "Fact-check explainer tracing the rumor and quoting poison-control experts.",
          whyItMatters: "Mainstream fact-check reinforcing the consensus.",
        },
        {
          title: "wellness-secrets.blog: My miracle cold cure",
          url: "https://www.google.com/search?q=does+drinking+bleach+cure+colds",
          type: "blog",
          date: "2024",
          stance: 0.4,
          reliability: 0.08,
          relevance: 0.5,
          summary: "Anecdotal personal blog post promoting the claim with no evidence.",
          whyItMatters:
            "Shows the lone low-reliability voice that the rumor rests on.",
        },
      ],
      votes: {
        trusted: votes(0, 7),
        public: votes(2, 41),
      },
    },
  },

  // B. CONFLICT — reliable sources support the claim, but the crowd votes false.
  //    Proves why surfacing human consensus matters. -> disputed
  {
    id: "conflict",
    label: "Conflict",
    blurb: "AI & evidence say true, but the crowd distrusts it.",
    claim: {
      text: "mRNA COVID-19 vaccines do not alter your DNA.",
      aiVerdict: {
        score: 93,
        reasoning:
          "mRNA never enters the cell nucleus and cannot integrate into the genome; regulators and peer-reviewed studies confirm it does not modify human DNA.",
      },
      sources: [
        {
          title: "CDC: Myths and Facts about COVID-19 Vaccines",
          url: "https://www.cdc.gov/",
          type: "gov",
          date: "2023",
          stance: 0.95,
          reliability: 0.96,
          relevance: 0.92,
          summary:
            "CDC explains mRNA stays in the cytoplasm and never interacts with DNA in the nucleus.",
          whyItMatters: "Authoritative, direct rebuttal of the DNA myth.",
        },
        {
          title: "Nature: mRNA vaccine mechanism of action",
          url: "https://www.nature.com/",
          type: "academic",
          date: "2022",
          stance: 0.92,
          reliability: 0.94,
          relevance: 0.88,
          summary:
            "Peer-reviewed review of how lipid-nanoparticle mRNA is translated then degraded.",
          whyItMatters: "Top-tier journal describing the biology.",
        },
        {
          title: "FDA: COVID-19 Vaccine Safety",
          url: "https://www.fda.gov/",
          type: "gov",
          date: "2023",
          stance: 0.9,
          reliability: 0.95,
          relevance: 0.85,
          summary: "Regulatory safety review; no mechanism for genomic integration.",
          whyItMatters: "Independent regulator confirming the same conclusion.",
        },
        {
          title: "AP News: Fact-checking the DNA claim",
          url: "https://apnews.com/",
          type: "news",
          date: "2021",
          stance: 0.7,
          reliability: 0.78,
          relevance: 0.7,
          summary: "Explainer interviewing molecular biologists.",
          whyItMatters: "Accessible reporting matching the science.",
        },
        {
          title: "viral post: 'They changed my genes!'",
          url: "https://www.google.com/search?q=do+mRNA+vaccines+alter+DNA",
          type: "social",
          date: "2024",
          stance: -0.6,
          reliability: 0.1,
          relevance: 0.55,
          summary:
            "High-engagement social post asserting DNA alteration without evidence.",
          whyItMatters: "The kind of content driving the crowd's contradicting vote.",
        },
      ],
      votes: {
        trusted: votes(2, 3),
        public: votes(9, 27),
      },
    },
  },

  // C. CONTESTED — opinion / developing topic. Sources scattered, crowd split.
  //    USCIS contradicts, Reddit supports. -> uncertain
  {
    id: "contested",
    label: "Contested",
    blurb: "Scattered sources, split crowd — honestly uncertain.",
    claim: {
      text: "F-1 students can work off-campus freely after one semester.",
      aiVerdict: {
        score: 31,
        reasoning:
          "Off-campus employment for F-1 students requires specific authorization (CPT or OPT) and is not automatically allowed after one semester; eligibility depends on the program and prior approval.",
      },
      sources: [
        {
          title: "USCIS: Students and Employment (F-1)",
          url: "https://www.uscis.gov/",
          type: "gov",
          date: "2024",
          stance: -0.85,
          reliability: 0.95,
          relevance: 0.9,
          summary:
            "Official rules: off-campus work needs CPT/OPT authorization, not granted automatically.",
          whyItMatters: "The governing authority on F-1 employment.",
        },
        {
          title: "University International Student Office (DSO) guidance",
          url: "https://oiss.yale.edu/",
          type: "academic",
          date: "2024",
          stance: -0.8,
          reliability: 0.85,
          relevance: 0.85,
          summary:
            "DSO explains students must apply and be approved before any off-campus work.",
          whyItMatters: "The advisor students are legally required to consult.",
        },
        {
          title: "Inside Higher Ed: Confusion over F-1 work rules",
          url: "https://www.insidehighered.com/",
          type: "news",
          date: "2023",
          stance: -0.3,
          reliability: 0.7,
          relevance: 0.6,
          summary:
            "Reports widespread misunderstanding; rules are conditional, not a flat ban or permission.",
          whyItMatters: "Adds nuance — the topic is genuinely confusing.",
        },
        {
          title: "r/f1visa thread: 'I worked after one semester'",
          url: "https://www.reddit.com/r/f1visa/",
          type: "social",
          date: "2024",
          stance: 0.5,
          reliability: 0.2,
          relevance: 0.5,
          summary:
            "Anecdotes from students who say they worked, often without describing authorization.",
          whyItMatters: "Where the off-campus 'freely' belief spreads.",
        },
        {
          title: "immigration-tips.blog: F-1 work hacks",
          url: "https://www.internationalstudent.com/",
          type: "blog",
          date: "2023",
          stance: 0.35,
          reliability: 0.3,
          relevance: 0.55,
          summary:
            "Blog overstating flexibility of off-campus work for international students.",
          whyItMatters: "Low-reliability source amplifying the claim.",
        },
      ],
      votes: {
        trusted: votes(1, 1),
        public: votes(14, 12),
      },
    },
  },
];
