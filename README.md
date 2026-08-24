Blockchain Investigation Without Enterprise Platforms: From an Address to a Relationship Graph with Python and Open Source

When we talk about Blockchain Investigation, we immediately think of professional platforms like Chainalysis Reactor, Crystal Expert, TRM Forensics, Elliptic Investigator, Merkle Science Tracker, or Scorechain.

These are highly advanced tools, capable not only of reconstructing the movements of digital assets, but also of adding a crucial element for the investigator: intelligence on the identity and nature of the entities behind blockchain addresses.

The most advanced platforms feature proprietary attribution databases, risk scoring systems, exchange and service identification, clustering heuristics, cross-chain tracing, bridge, swap, and mixer analysis, case management tools, and functions to produce results usable in structured investigations.

But what happens when an analyst, researcher, student, or small investigative team doesn't have these enterprise platforms?

An important part of the analysis can be achieved by leveraging a fundamental feature of public blockchains:

Transactional data is public, queryable, and structureable.

## From wallet to graph

Starting from this principle, I published the project **tronAnalisys** on GitHub, dedicated to analyzing flows on the **TRON** blockchain, a network particularly interesting due to the widespread use of TRC20 tokens like USDT.

The project's goal is quite simple: starting from one or more known addresses and transforming blockchain transactions into an **investigative graph**.

The Python program uses the **TronGrid** API and can integrate information from **TronScan**. Starting from the seed addresses, it builds a `MultiDiGraph` with NetworkX and performs a BFS (Breadth First Search) expansion with configurable depth. It can then reconstruct the first level of relationships, the second level, or additional levels, following incoming or outgoing transactions, or both.

In other words:

**Wallet A → first-tier counterparties → second-tier counterparties → ...**

Of course, we're talking about the connections **observable on-chain within the scope, asset, and timeframe defined by the analyst**.

The program manages TRX and TRC20 transfers, allows thresholds to be applied to amounts, aggregates transactions between the same counterparties, and calculates total incoming and outgoing values ​​for each node. It can also acquire public tags and indicators available via TronScan and recognize nodes classified as exchanges, services, or bridges.

This last aspect is particularly important.

A centralized exchange or bridge often represents a sort of **investigative boundary**: the asset can continue its journey, but correctly attributing its subsequent movement may require off-chain data, information provided by the VASP, or professional cross-chain intelligence systems.

And this is precisely where the difference between **blockchain analysis** and **blockchain intelligence** emerges.

## Python as a Graph Analysis Engine

Once the transactions are collected, the problem is no longer necessarily "crypto."

It becomes a classic **Network Analysis** problem.

Addresses are nodes.

Transactions are edges.

Amounts, transaction frequency, assets, and timestamps become graph attributes.

For this project, I chose **NetworkX**, which allows not only to represent the network but also to apply graph analytics algorithms.

This opens up a very interesting second level of analysis: **community detection and network clustering**.

NetworkX offers, for example, algorithms such as Louvain, label propagation, Girvan-Newman, and other tools for identifying structures and communities within a graph. Louvain searches for groups of nodes characterized by a higher density of internal connections by optimizing modularity. Implementations and backends for algorithms such as Leiden are also available.

This allows us to move from a simple representation of transactions to more interesting questions:

*Which addresses constitute a highly interconnected community? Which nodes act as hubs? Which addresses act as bridges between different clusters? Which nodes have high centrality? Where is the greatest economic flow concentrated?*

However, a fundamental methodological clarification is necessary:

**A mathematically identified community does not automatically equate to a cluster of addresses belonging to the same person or organization.**

Community detection generates an analytical hypothesis based on the structure of relationships. Attribution requires additional evidence, specific heuristics, OSINT, and off-chain data.
