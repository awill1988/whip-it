use serde::Deserialize;
use serde_json::{Value, json};

const DIMENSIONS: usize = 64;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Model {
    pub schema_version: u32,
    pub encoder: String,
    pub window_days: u32,
    pub threshold_multiplier: u32,
    pub weights: [Vec<f64>; 2],
    pub bias: [f64; 2],
}

pub fn encode(plan: &str) -> [f64; DIMENSIONS] {
    let mut features = [0.0_f64; DIMENSIONS];
    for word in plan
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
    {
        let hash = word
            .to_lowercase()
            .bytes()
            .fold(0xcbf29ce484222325_u64, |hash, byte| {
                (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
            });
        features[hash as usize % DIMENSIONS] += 1.0;
    }
    let norm = features.iter().map(|x| x * x).sum::<f64>().sqrt();
    if norm > 0.0 {
        for feature in &mut features {
            *feature /= norm;
        }
    }
    features
}

pub fn classify(input: &Value, model: Option<&Model>) -> Result<Value, &'static str> {
    if input.get("estimated_cost").is_some() || input.get("normal_cost").is_some() {
        let estimate = input["estimated_cost"]
            .as_f64()
            .filter(|x| x.is_finite() && *x >= 0.0)
            .ok_or("invalid cost estimate")?;
        let baseline = input["normal_cost"]
            .as_f64()
            .filter(|x| x.is_finite() && *x > 0.0)
            .ok_or("invalid cost baseline")?;
        let ratio = estimate / baseline;
        if !ratio.is_finite() {
            return Err("invalid cost ratio");
        }
        return Ok(
            json!({"classification":if ratio > 3.0 { "over_3x" } else { "within_3x" },
            "source":"supplied_estimate", "cost_ratio":ratio, "threshold_multiplier":3,
            "action":"allow"}),
        );
    }
    let Some(model) = model else {
        return Ok(
            json!({"classification":"insufficient_data", "reason_code":"model_unavailable", "action":"allow"}),
        );
    };
    if model.schema_version != 1
        || model.encoder != "fnv1a_words_l2_v1"
        || model.window_days != 7
        || model.threshold_multiplier != 3
        || model
            .weights
            .iter()
            .any(|row| row.len() != DIMENSIONS || row.iter().any(|w| !w.is_finite()))
        || model.bias.iter().any(|x| !x.is_finite())
    {
        return Err("invalid classifier model");
    }
    let plan = input["plan"].as_str().ok_or("missing plan")?;
    let features = encode(plan);
    if features.iter().all(|x| *x == 0.0) {
        return Ok(
            json!({"classification":"insufficient_data", "reason_code":"empty_features", "action":"allow"}),
        );
    }
    let logits = model.weights.each_ref().map(|row| {
        row.iter()
            .zip(features)
            .map(|(weight, feature)| weight * feature)
            .sum::<f64>()
    });
    let logits = [logits[0] + model.bias[0], logits[1] + model.bias[1]];
    if logits.iter().any(|x| !x.is_finite()) {
        return Err("invalid classifier score");
    }
    let max = logits[0].max(logits[1]);
    let exp = logits.map(|x| (x - max).exp());
    let scores = exp.map(|x| x / (exp[0] + exp[1]));
    Ok(
        json!({"classification":if logits[1] > logits[0] { "over_3x" } else if logits[0] > logits[1] { "within_3x" } else { "insufficient_data" },
        "source":"local_model", "encoder":model.encoder, "window_days":7,
        "threshold_multiplier":3, "score_kind":"uncalibrated_softmax",
        "scores":{"within_3x":scores[0], "over_3x":scores[1]}, "action":"allow"}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_three_times_boundary() {
        for (estimate, expected) in [
            (299.0, "within_3x"),
            (300.0, "within_3x"),
            (301.0, "over_3x"),
        ] {
            assert_eq!(
                classify(&json!({"estimated_cost":estimate,"normal_cost":100}), None).unwrap()["classification"],
                expected
            );
        }
        assert!(classify(&json!({"estimated_cost":1,"normal_cost":0}), None).is_err());
    }

    #[test]
    fn stable_softmax_and_unknown_model() {
        assert_eq!(
            classify(&json!({"plan":"test"}), None).unwrap()["classification"],
            "insufficient_data"
        );
        let model = Model {
            schema_version: 1,
            encoder: "fnv1a_words_l2_v1".into(),
            window_days: 7,
            threshold_multiplier: 3,
            weights: [vec![0.0; DIMENSIONS], vec![0.0; DIMENSIONS]],
            bias: [10000.0, 10001.0],
        };
        let result = classify(&json!({"plan":"private plan"}), Some(&model)).unwrap();
        assert_eq!(result["classification"], "over_3x");
        assert!((result["scores"]["over_3x"].as_f64().unwrap() - 0.7310585786).abs() < 1e-9);
        assert!(!result.to_string().contains("private"));
        assert_eq!(
            classify(&json!({"plan":""}), Some(&model)).unwrap()["classification"],
            "insufficient_data"
        );
    }

    #[test]
    fn encoding_is_repeatable_and_normalized() {
        assert_eq!(encode("Run TEST"), encode("test run"));
        assert!((encode("a b c").iter().map(|x| x * x).sum::<f64>() - 1.0).abs() < 1e-12);
    }

    #[test]
    fn rejects_invalid_weights_and_abstains_on_ties() {
        let mut model = Model {
            schema_version: 1,
            encoder: "fnv1a_words_l2_v1".into(),
            window_days: 7,
            threshold_multiplier: 3,
            weights: [vec![0.0; DIMENSIONS], vec![0.0; DIMENSIONS]],
            bias: [0.0; 2],
        };
        let input = json!({"plan":"test"});
        assert_eq!(
            classify(&input, Some(&model)).unwrap()["classification"],
            "insufficient_data"
        );
        model.weights[0][0] = f64::NAN;
        assert!(classify(&input, Some(&model)).is_err());
        model.weights[0] = vec![0.0; 63];
        assert!(classify(&input, Some(&model)).is_err());
        assert!(classify(&json!({"estimated_cost":-1,"normal_cost":10}), None).is_err());
    }
}
