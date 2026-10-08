// Prints the wall clock as Unix seconds, so a run can tell whether a clock shift reaches Rust programs.
fn main() {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64);
    let seconds = match now {
        Ok(s) => s,
        Err(e) => -(e.duration().as_secs() as i64),
    };
    println!("{}", seconds);
}
