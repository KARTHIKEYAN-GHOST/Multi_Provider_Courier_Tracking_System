//! RabbitMQ topology, read from `courier-contracts/topology.yaml` — the same file the Python
//! workers read. The file is compiled into the binary, so the Engine and its topology can
//! never be deployed out of step.
//!
//! The Engine is the only component that declares exchanges and queues. Workers have no
//! `configure` permission and only look their queue up.

use std::collections::BTreeMap;

use serde::Deserialize;

const TOPOLOGY_YAML: &str = include_str!("../../../courier-contracts/topology.yaml");

pub const EVENT_CONSUMER: &str = "engine-event-consumer";
pub const DEAD_LETTER_CONSUMER: &str = "engine-dead-letter-consumer";

#[derive(Debug, Clone, Deserialize)]
pub struct Topology {
    pub exchanges: Vec<Exchange>,
    pub queues: Vec<Queue>,
    /// Prepended to every exchange and queue name. Empty in production; integration tests
    /// use a unique prefix so they never touch the real queues or each other.
    #[serde(skip)]
    pub prefix: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Exchange {
    pub name: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub durable: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Queue {
    pub name: String,
    pub durable: bool,
    pub consumer: String,
    #[serde(default)]
    pub arguments: BTreeMap<String, serde_yaml_ng::Value>,
    #[serde(default)]
    pub bindings: Vec<Binding>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Binding {
    pub exchange: String,
    pub routing_key: String,
}

/// A queue argument, reduced to the two types RabbitMQ needs here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Argument {
    Text(String),
    Number(i64),
}

impl Topology {
    /// The topology shared with the workers.
    pub fn load() -> Result<Self, String> {
        Self::parse(TOPOLOGY_YAML)
    }

    pub fn parse(yaml: &str) -> Result<Self, String> {
        let topology: Topology =
            serde_yaml_ng::from_str(yaml).map_err(|e| format!("invalid topology.yaml: {e}"))?;
        for queue in &topology.queues {
            for value in queue.arguments.values() {
                if !(value.is_string() || value.is_i64()) {
                    return Err(format!(
                        "queue {}: arguments must be strings or integers",
                        queue.name
                    ));
                }
            }
        }
        Ok(topology)
    }

    pub fn with_prefix(mut self, prefix: &str) -> Self {
        self.prefix = prefix.to_string();
        self
    }

    /// The name of an exchange or queue as it exists on the broker.
    pub fn physical(&self, logical: &str) -> String {
        format!("{}{logical}", self.prefix)
    }

    /// Queue arguments ready to declare. The dead-letter exchange is a name, so it gets the
    /// prefix; routing keys do not.
    pub fn arguments(&self, queue: &Queue) -> Vec<(String, Argument)> {
        queue
            .arguments
            .iter()
            .map(|(key, value)| {
                let argument = match (value.as_str(), value.as_i64()) {
                    (Some(text), _) if key == "x-dead-letter-exchange" => {
                        Argument::Text(self.physical(text))
                    }
                    (Some(text), _) => Argument::Text(text.to_string()),
                    (_, Some(number)) => Argument::Number(number),
                    _ => unreachable!("validated in parse"),
                };
                (key.clone(), argument)
            })
            .collect()
    }

    /// Logical names of the queues a given Engine consumer reads.
    pub fn queues_for(&self, consumer: &str) -> Vec<&Queue> {
        self.queues
            .iter()
            .filter(|q| q.consumer == consumer)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_shared_topology_file_loads_and_names_the_engine_queues() {
        let topology = Topology::load().unwrap();
        assert_eq!(topology.exchanges.len(), 3);
        assert!(
            topology
                .exchanges
                .iter()
                .all(|e| e.kind == "topic" && e.durable)
        );

        let events = topology.queues_for(EVENT_CONSUMER);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "engine.events");
        // Every adapter's job queue has a dead-letter queue the Engine consumes, and
        // nothing else is consumed as one. (The list grows when adapters are added.)
        let dead: Vec<&str> = topology
            .queues_for(DEAD_LETTER_CONSUMER)
            .iter()
            .map(|q| q.name.as_str())
            .collect();
        let expected: Vec<String> = topology
            .queues
            .iter()
            .filter(|q| q.consumer == "adapter-worker")
            .map(|q| format!("{}.dlq", q.name))
            .collect();
        assert_eq!(dead, expected);
        assert!(dead.contains(&"adapter.dummy.jobs.dlq"));
        // The parked queue has no Engine consumer.
        assert!(
            topology
                .queues
                .iter()
                .any(|q| q.name == "engine.events.dlq" && q.consumer == "none")
        );
    }

    #[test]
    fn a_prefix_applies_to_names_but_not_to_routing_keys() {
        let topology = Topology::load().unwrap().with_prefix("t1.");
        let jobs = topology
            .queues
            .iter()
            .find(|q| q.name == "adapter.dummy.jobs")
            .unwrap();
        let arguments: BTreeMap<_, _> = topology.arguments(jobs).into_iter().collect();
        assert_eq!(arguments["x-queue-type"], Argument::Text("quorum".into()));
        assert_eq!(arguments["x-delivery-limit"], Argument::Number(3));
        assert_eq!(
            arguments["x-dead-letter-exchange"],
            Argument::Text("t1.courier.dlx".into())
        );
        assert_eq!(
            arguments["x-dead-letter-routing-key"],
            Argument::Text("adapter.dummy.jobs".into())
        );
        assert_eq!(topology.physical("engine.events"), "t1.engine.events");
    }
}
