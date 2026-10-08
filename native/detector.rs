use regex::Regex;
use serde::{Deserialize, Serialize};
use std::sync::LazyLock;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Limits {
    pub subagents_allowed: bool,
    pub max_subagents: Option<u64>,
    pub force_simplify: bool,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub detected_phrase: Option<String>,
}

static ZERO: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [
    r"\b(?:no|without|zero)\s+sub[-_\s]?agents?\b",
    r"\bdo(?:n't|\s+not)\s+(?:use|spawn|create|invoke|start|launch|spin\s+up)\s+(?:any\s+)?sub[-_\s]?agents?\b",
    r"\bdo(?:n't|\s+not)\s+delegate\b",
    r"\bno\s+(?:agent\s+)?delegation\b",
    r"\bwithout\s+delegat(?:ing|ion)\b",
    r"\bsingle\s+agent(?:\s+only)?\b",
    r"\bkeep\s+it\s+simple\b",
    r"\bkeep\s+things\s+simple\b",
    r"\bexecute\s+directly\b",
    r"\bsolve\s+(?:this\s+)?directly\b",
    r"\bdo\s+(?:this|it)\s+directly\b",
    r"\bwork\s+directly\s+in\s+this\s+(?:session|thread|context)\b",
    r"\bstay\s+in\s+this\s+(?:session|thread|context)\b",
    r"\bno\s+(?:agent\s+)?swarms?\b",
    r"\bno\s+child\s+agents?\b",
    r"\bdo(?:n't|\s+not)\s+spawn\s+swarms?\b",
    r"\badhere\s+to\s+limits\b",
].iter().map(|p| Regex::new(p).expect("constant pattern")).collect()
});

static QUOTA: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [
        r"\blimit\s+to\s+(?:at\s+most\s+)?([0-9]+)\s+sub[-_\s]?agents?\b",
        r"\bat\s+most\s+([0-9]+)\s+sub[-_\s]?agents?\b",
        r"\bmax(?:imum)?\s+(?:of\s+)?([0-9]+)\s+sub[-_\s]?agents?\b",
        r"\b([0-9]+)\s+sub[-_\s]?agents?\s+(?:max|maximum|limit)\b",
        r"\bno\s+more\s+than\s+([0-9]+)\s+sub[-_\s]?agents?\b",
        r"\buse\s+at\s+most\s+(?:one|1)\s+sub[-_\s]?agent\b",
        r"\bat\s+most\s+one\s+sub[-_\s]?agent\b",
        r"\blimit\s+to\s+one\s+sub[-_\s]?agent\b",
    ]
    .iter()
    .map(|p| Regex::new(p).expect("constant pattern"))
    .collect()
});

pub fn analyze(prompt: &str) -> Limits {
    let lower = prompt.to_lowercase();
    for pattern in ZERO.iter() {
        if let Some(found) = pattern.find(&lower) {
            return Limits {
                subagents_allowed: false,
                max_subagents: Some(0),
                force_simplify: true,
                detected_phrase: Some(found.as_str().into()),
            };
        }
    }
    for pattern in QUOTA.iter() {
        if let Some(found) = pattern.captures(&lower) {
            let quota = found
                .get(1)
                .map_or(1, |n| n.as_str().parse().unwrap_or(u64::MAX));
            return Limits {
                subagents_allowed: quota > 0,
                max_subagents: Some(quota),
                force_simplify: quota == 0,
                detected_phrase: Some(found[0].into()),
            };
        }
    }
    Limits {
        subagents_allowed: true,
        max_subagents: None,
        force_simplify: false,
        detected_phrase: None,
    }
}
