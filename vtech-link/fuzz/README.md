# vtech-link fuzzing (T5)

One cargo-fuzz target per network parser or state machine in `vtech-link-core`: `iotc_decode`,
`iotc_session`, `dtls_client`, `av_packet`, `av_client`, `media_rx`, `control_stream`, `fec`,
`av_server`, `media_key`. Each asserts "doesn't panic" plus a cheap invariant where one exists.
This directory is its own workspace on the nightly toolchain (`rust-toolchain.toml`), so the main
workspace stays on the pinned stable one.

    export PATH=$HOME/.cargo/bin:$PATH      # Fedora's /usr/bin/cargo shadows rustup
    cargo install cargo-fuzz --locked; rustup toolchain install nightly --profile minimal
    ./seed.sh                                # seeds corpus/ from core/tests/fixtures/tutk_host.txt
    cargo +nightly fuzz run iotc_decode      # until a crash; artifacts/ gets the input
    for t in $(cargo +nightly fuzz list); do cargo +nightly fuzz run $t -- -max_total_time=60 || break; done  # CI smoke
    cargo +nightly fuzz tmin <target> artifacts/<target>/crash-...   # minimise a crash

`corpus/`, `artifacts/` and `target/` are gitignored; `Cargo.lock` is a copy of the workspace's.
