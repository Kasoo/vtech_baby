# vtech-link

The native transport of the [vtech_baby](https://gitlab.com/jonathanduncan/vtech_baby) Home Assistant
integration: a Rust implementation of the LAN-direct sessions the VTech RM5754HD baby monitor speaks, as the
Python module `_vtech_link`. It sends only to directly attached subnets and has no P2P or relay path.

Home Assistant installs it for the integration; its API isn't meant for other use and may change between
versions.

Not affiliated with VTech or ThroughTek.
