// Host-side shim for <ESPAsyncTCP.h> (Tankie unit tests, issue #34).
//
// tankie.ino includes it (indirectly required by the async web stack).
// The host tests never open a TCP socket - this file only exists so the
// include resolves.
#pragma once
