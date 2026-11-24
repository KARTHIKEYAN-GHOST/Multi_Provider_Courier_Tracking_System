#[tokio::main]
async fn main() -> anyhow::Result<()> {
    courier_engine::cli::run().await
}
