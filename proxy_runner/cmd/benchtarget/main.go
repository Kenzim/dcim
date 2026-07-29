// benchtarget is a minimal, fast HTTP server used as the fixed destination
// for proxy latency/concurrency benchmarks, so results measure proxy
// overhead rather than internet/upstream variance.
package main

import (
	"log"
	"net/http"
)

func main() {
	body := []byte(`{"ok":true}`)
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", "11")
		_, _ = w.Write(body)
	})
	srv := &http.Server{
		Addr:    ":9999",
		Handler: mux,
	}
	log.Println("benchtarget listening on :9999")
	log.Fatal(srv.ListenAndServe())
}
