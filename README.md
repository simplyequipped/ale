# ale

Non-standard ALE ([automatic link establishment](https://en.wikipedia.org/wiki/Automatic_Link_Establishment)) package with similar goals to traditional ALE (reducing the operator skill required to establish a communication link in the ever-changing HF environment which is dependant on atmospheric conditions) but designed to function in a resource limited environement such as a Raspberry Pi. The package includes support for single and group addresses, multiple scan lists, automatic call acknowledgement handshaking, automatic link quality analysis via channel sounding, and data packet pass-through to a parent application once a connection is established.

Only the [QRP Labs QDX tranceiver](http://qrp-labs.com/qdx) (via the [qdx](https://github.com/simplyequipped/qdx) package) and the [fskmodem](https://github.com/simplyequipped/fskmodem) software-based packet modem (default 300 baud for HF packet) are supported currently.

**WARNING: this package is still in development and largely untested**
