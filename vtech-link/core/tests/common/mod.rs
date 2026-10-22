//! Loads `fixtures/tutk_host.txt`: known-answer bytes generated from tutk-host.

use std::collections::HashMap;

pub struct Fixtures(HashMap<String, Vec<u8>>);

impl Fixtures {
    pub fn load() -> Self {
        let text = include_str!("../fixtures/tutk_host.txt");
        let map = text
            .lines()
            .filter(|l| !l.starts_with('#') && !l.trim().is_empty())
            .map(|l| {
                let (name, hex) = l.split_once(" = ").expect("name = hex");
                (name.to_string(), unhex(hex))
            })
            .collect();
        Self(map)
    }

    pub fn get(&self, name: &str) -> &[u8] {
        self.0.get(name).unwrap_or_else(|| panic!("no fixture {name}"))
    }
}

pub fn unhex(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect()
}
