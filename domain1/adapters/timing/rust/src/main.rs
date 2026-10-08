//! The in-process timing loop for the Rust assembler (domain-1-plan.md, 8.3, method 3).
//!
//! Snapshot bytes on stdin; arguments: warm-up runs, then timed runs. Each run goes from the snapshot's bytes to the
//! assembler's payload and trace, as the adapter does, without starting a process. Prints one JSON line:
//! {"outcome": "assembled" | "refused" | "rejected" | "unsupported", "samples_ns": [...]}.

use std::io::Read;
use std::time::Instant;

use contextwindowarchitecture_assembler::{assemble, Error};

fn once(raw: &[u8]) -> &'static str {
    match assemble(raw) {
        Ok(assembly) if assembly.payload.is_some() => "assembled",
        Ok(_) => "refused",
        Err(Error::Rejected(_)) => "rejected",
        Err(_) => "unsupported",
    }
}

fn main() {
    let args: Vec<usize> = std::env::args().skip(1).map(|a| a.parse().expect("a count")).collect();
    let (warmup, runs) = (args[0], args[1]);
    let mut raw = Vec::new();
    std::io::stdin().read_to_end(&mut raw).expect("stdin is readable");
    let mut outcome = "";
    for _ in 0..warmup {
        outcome = once(&raw);
    }
    let mut samples = Vec::with_capacity(runs);
    for _ in 0..runs {
        let started = Instant::now();
        outcome = once(&raw);
        samples.push(started.elapsed().as_nanos().to_string());
    }
    println!("{{\"outcome\":\"{outcome}\",\"samples_ns\":[{}]}}", samples.join(","));
}
