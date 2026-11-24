//! Courier tracking Engine.
//!
//! The Engine owns the database and all business logic. Adapters never touch these tables;
//! they exchange messages with the Engine through RabbitMQ (wired up in Phase 3).

pub mod api;
pub mod cli;
pub mod config;
pub mod db;
pub mod domain;
pub mod error;
pub mod messaging;
pub mod services;
pub mod state;
pub mod telemetry;
