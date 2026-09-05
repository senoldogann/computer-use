#[path = "../../driver/src/bezier.rs"]
mod bezier;

fn main() {
    let from: bezier::Point = bezier::point(0, 0);
    let to: bezier::Point = bezier::point(1200, 0);
    let requested: u64 = 1000;
    let duration: u64 = bezier::human_move_duration(from, to, requested);
    let trajectory: Vec<(bezier::Point, std::time::Duration)> = bezier::plan_trajectory(from, to, duration, 16);
    let total: u128 = trajectory.iter().map(|(_, pause): &(bezier::Point, std::time::Duration)| pause.as_millis()).sum();
    println!("requested_ms={requested} planned_ms={duration} total_delays_ms={total}");
    println!("delays={:?}", trajectory.iter().map(|(_, pause): &(bezier::Point, std::time::Duration)| pause.as_millis()).collect::<Vec<u128>>());
}
