// Command sbs is the Skillberry Store CLI.
//
// This file is deliberately the whole of the `main` package: every line of
// behaviour lives in the importable `cli` package next door
// (github.com/skillberry-ai/skillberry-store/client/go/cli), which is what lets
// the test suite live in client/go/tests.
//
// Go requires that split. A `package main` is "a program, not an importable
// package", so a test in any other directory cannot reach it — the compiler
// refuses the import outright. Keeping main to an entry point therefore is not
// style, it is the precondition for the repository layout.
//
// See docs/design/new_cli.md for the design and client/go/cli for the code.
package main

import (
	"errors"
	"fmt"
	"os"

	"github.com/skillberry-ai/skillberry-store/client/go/cli"
)

func main() {
	err := cli.Run(os.Args, os.Stdout, os.Stderr)

	// A verb that chose its own exit status (bad usage is 2, `login` against a
	// store with auth disabled is 2) reports it as an error value rather than
	// calling os.Exit itself, so that cli.Run stays testable without ending the
	// test process.
	var ec cli.ExitCode
	if errors.As(err, &ec) {
		os.Exit(int(ec))
	}
	if err != nil {
		// A single-line error. restish already prints operation-level failures
		// itself; this catches the wiring-level ones.
		fmt.Fprintf(os.Stderr, "%s: %v\n", cli.CLIName, err)
		os.Exit(1)
	}
}
