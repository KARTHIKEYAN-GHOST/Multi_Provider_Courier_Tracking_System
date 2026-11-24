//! Pure domain types: no I/O.

pub mod status;
pub mod tracking_number;

pub use status::{
    ErrorClass, ExecutionStatus, JobOutcome, JobTrigger, ShipmentStatus, TrackingMethod,
};
