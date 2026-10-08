// Command cwabench-timing is the in-process timing loop for the Go assembler (domain-1-plan.md, 8.3, method 3).
// The benchmark builds it into the checkout's module through `go build -overlay`, so the checkout is not modified.
//
// Snapshot bytes on stdin; arguments: warm-up runs, then timed runs. Each run goes from the snapshot's bytes to the
// assembler's payload and trace, as the adapter does, without starting a process. Prints one JSON line:
// {"outcome": "assembled" | "refused" | "rejected" | "unsupported", "samples_ns": [...]}.
package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"strconv"
	"time"

	assembler "github.com/contextwindowarchitecture/assembler-go"
)

func once(raw []byte) string {
	result, err := assembler.Assemble(raw, assembler.Options{})
	if err != nil {
		var rejected *assembler.SnapshotRejectedError
		if errors.As(err, &rejected) {
			return "rejected"
		}
		return "unsupported"
	}
	if result.Payload == nil {
		return "refused"
	}
	return "assembled"
}

func main() {
	warmup, _ := strconv.Atoi(os.Args[1])
	runs, _ := strconv.Atoi(os.Args[2])
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		os.Exit(1)
	}
	outcome := ""
	for i := 0; i < warmup; i++ {
		outcome = once(raw)
	}
	samples := make([]int64, 0, runs)
	for i := 0; i < runs; i++ {
		started := time.Now()
		outcome = once(raw)
		samples = append(samples, time.Since(started).Nanoseconds())
	}
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"outcome": outcome, "samples_ns": samples})
}
