# prima-pool-server

A server for an AI inference pool based on prima.cpp.

# Abstract
Large Language Models are often too large to run on a single machine, leading people to attempt to spread models across multiple machines. One of these attempts led to the creation of prima.cpp, which spreads a model across multiple machines connected to a local network.
Since prima.cpp can lead to usable performance through the internet (I personally experienced ~10-15 tokens/s on two machines connected through Tailscale with ~50ms latency), this repo aims to explore a broader use of prima.cpp, allowing to pool compute from crowdsourced machines.

The goal of this repo will be the creation of a control plane above prima.cpp, overseeing multiple distributed clusters and accounting for token usage. This would enable the creation of an accounting model similar to the one of cryptocurrency mining pools (e.g. a rate per token*layer) for operators with a billing similar to standard API billing (deposit money => make API call => it works).

## Strengths

This repository aims to decentralize AI compute, allowing people with smaller hardware to participate and provide computing power.

Additionally, while allowing AI accounting, it would make decentralized AI economically viable: while decentralized open-source options like Petals exist, their growth is limited by the fact that hardware is expensive and electricity doesn't fall from the sky (I mean, it kinda does, but not in a practical nor consistent way).

## Downsides

While prima.cpp has yielded to an usable performance with two devices through a tailscale link, a lower performance is to be expected with more devices, as clusters become more network-bound than compute-bound.

Additionally, decentralizing inference leads to privacy challenges, since multiple third-party providers end up processing one user's prompt.

# Target workflows

## For compute providers

- provider installs the prima-pool software on their computer (which would expose the required prima ports through a WireGuard tunnel)
- they configure it for a specific model
- pool adds it to a model-specific waitlist (noted model.waitlist here)
- if sum(computer.memory_allocated for computer in model.waitlist) >= model.required_memory, it takes all the waitlisted computers and groups them in a prima.cpp cluster, which becomes available for jobs
- when a request is procesed by the cluster, the provider's balance accrues

## For users

- user sends a request through the OpenAI api
- it's routed to a cluster, with a routing algorithm yet to specify