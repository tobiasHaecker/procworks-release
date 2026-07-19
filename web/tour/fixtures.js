// SPDX-License-Identifier: BUSL-1.1
// ---------------------------------------------------------------------------
// Schnappschuesse des Tutorial-Beispielprozesses ("geführte Tour").
//
// ERZEUGT -- NICHT VON HAND BEARBEITEN.
//   Neu erzeugen mit:  cd core && python -m tests.tour_fixture_build
//   Quelle:            core/tests/tour_fixture_build.py
//
// Waehrend der Tour laeuft der Web-Client im schreibfreien Sandkasten: Kein
// POST/PUT/DELETE verlaesst den Browser, und jeder /schemas-Aufruf wird aus
// diesen Stufen bedient. Dadurch entstehen durch Tutorial-Eingaben keine
// dauerhaften Daten (docs/Tutorial-Konzept.md, §4).
//
// Es ist eine AUFZEICHNUNG, keine Logik: Der Client wertet hier nichts aus, er
// spielt ab, was der Kern in dieser Situation geliefert haette. Der Wachtest
// core/tests/test_tour_web.py faehrt die Stufen gegen den echten Kern nach und
// schlaegt fehl, sobald die Aufzeichnung von ihm abweicht.
//
// Stufe 0 = Ausgangslage · 1 = nach dem Einfuegen · 2 = nach der Datenbindung
// Stufe 3 = nach der Bearbeiterbindung (Befund aus Stufe 1 wieder aufgeloest).
// ---------------------------------------------------------------------------

const TOUR_FIXTURES = {
  "rejection": [
    {
      "message": "mandatory input 'Urlaubstage' may be read before it is written on some execution path",
      "node_id": "act_1",
      "rule": "D1"
    }
  ],
  "stages": [
    {
      "schema": {
        "activity_templates": {},
        "connectors": {},
        "data_accesses": [
          {
            "element_id": "tage",
            "mandatory": true,
            "mode": "WRITE",
            "node_id": "act_2",
            "param_type": null
          },
          {
            "element_id": "tage",
            "mandatory": true,
            "mode": "READ",
            "node_id": "act_3",
            "param_type": null
          }
        ],
        "data_elements": {
          "tage": {
            "data_type": "INTEGER",
            "external": null,
            "id": "tage",
            "name": "Urlaubstage",
            "select": null,
            "source": "INSTANCE",
            "write": null
          }
        },
        "deadline_seconds": null,
        "edges": [
          {
            "condition": null,
            "source": "start",
            "target": "act_2",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "act_2",
            "target": "act_3",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "act_3",
            "target": "end",
            "type": "CONTROL"
          }
        ],
        "follow_up_links": [],
        "forms": {},
        "id": "tour-schema",
        "is_library_subprocess": false,
        "lifecycle_state": "ENTWURF",
        "mail_bindings": {},
        "name": "Urlaubsantrag (Tutorial)",
        "node_priorities": {},
        "nodes": {
          "act_2": {
            "id": "act_2",
            "label": "Antrag erfassen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "act_3": {
            "id": "act_3",
            "label": "Antrag prüfen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "end": {
            "id": "end",
            "label": "Ende",
            "type": "END",
            "value_class": null
          },
          "start": {
            "id": "start",
            "label": "Start",
            "type": "START",
            "value_class": null
          }
        },
        "org_model": {
          "agents": {
            "tour-a-erika": {
              "deputy_id": null,
              "email": null,
              "id": "tour-a-erika",
              "name": "Erika Sander",
              "org_unit_id": "personal",
              "role_ids": [
                "sachbearbeiter"
              ]
            },
            "tour-a-tom": {
              "deputy_id": null,
              "email": null,
              "id": "tour-a-tom",
              "name": "Tom Berger",
              "org_unit_id": "personal",
              "role_ids": [
                "teamleitung"
              ]
            }
          },
          "id": null,
          "name": "",
          "org_units": {
            "personal": {
              "id": "personal",
              "mailbox": null,
              "manager_id": "tour-a-tom",
              "name": "Personal",
              "parent_id": null
            }
          },
          "roles": {
            "sachbearbeiter": {
              "id": "sachbearbeiter",
              "mailbox": null,
              "name": "Sachbearbeiter"
            },
            "teamleitung": {
              "id": "teamleitung",
              "mailbox": null,
              "name": "Teamleitung"
            }
          }
        },
        "org_model_id": null,
        "service_bindings": {},
        "staff_rules": {
          "act_2": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "sachbearbeiter"
          },
          "act_3": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "teamleitung"
          }
        },
        "sub_process_bindings": {},
        "time_constraints": {},
        "version": 1,
        "xor_decisions": {}
      },
      "validation": {
        "correct": true,
        "findings": []
      }
    },
    {
      "schema": {
        "activity_templates": {},
        "connectors": {},
        "data_accesses": [
          {
            "element_id": "tage",
            "mandatory": true,
            "mode": "WRITE",
            "node_id": "act_2",
            "param_type": null
          },
          {
            "element_id": "tage",
            "mandatory": true,
            "mode": "READ",
            "node_id": "act_3",
            "param_type": null
          }
        ],
        "data_elements": {
          "tage": {
            "data_type": "INTEGER",
            "external": null,
            "id": "tage",
            "name": "Urlaubstage",
            "select": null,
            "source": "INSTANCE",
            "write": null
          }
        },
        "deadline_seconds": null,
        "edges": [
          {
            "condition": null,
            "source": "act_2",
            "target": "act_3",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "act_3",
            "target": "end",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "start",
            "target": "act_4",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "act_4",
            "target": "act_2",
            "type": "CONTROL"
          }
        ],
        "follow_up_links": [],
        "forms": {},
        "id": "tour-schema",
        "is_library_subprocess": false,
        "lifecycle_state": "ENTWURF",
        "mail_bindings": {},
        "name": "Urlaubsantrag (Tutorial)",
        "node_priorities": {},
        "nodes": {
          "act_2": {
            "id": "act_2",
            "label": "Antrag erfassen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "act_3": {
            "id": "act_3",
            "label": "Antrag prüfen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "act_4": {
            "id": "act_4",
            "label": "Resturlaub prüfen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "end": {
            "id": "end",
            "label": "Ende",
            "type": "END",
            "value_class": null
          },
          "start": {
            "id": "start",
            "label": "Start",
            "type": "START",
            "value_class": null
          }
        },
        "org_model": {
          "agents": {
            "tour-a-erika": {
              "deputy_id": null,
              "email": null,
              "id": "tour-a-erika",
              "name": "Erika Sander",
              "org_unit_id": "personal",
              "role_ids": [
                "sachbearbeiter"
              ]
            },
            "tour-a-tom": {
              "deputy_id": null,
              "email": null,
              "id": "tour-a-tom",
              "name": "Tom Berger",
              "org_unit_id": "personal",
              "role_ids": [
                "teamleitung"
              ]
            }
          },
          "id": null,
          "name": "",
          "org_units": {
            "personal": {
              "id": "personal",
              "mailbox": null,
              "manager_id": "tour-a-tom",
              "name": "Personal",
              "parent_id": null
            }
          },
          "roles": {
            "sachbearbeiter": {
              "id": "sachbearbeiter",
              "mailbox": null,
              "name": "Sachbearbeiter"
            },
            "teamleitung": {
              "id": "teamleitung",
              "mailbox": null,
              "name": "Teamleitung"
            }
          }
        },
        "org_model_id": null,
        "service_bindings": {},
        "staff_rules": {
          "act_2": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "sachbearbeiter"
          },
          "act_3": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "teamleitung"
          }
        },
        "sub_process_bindings": {},
        "time_constraints": {},
        "version": 1,
        "xor_decisions": {}
      },
      "validation": {
        "correct": true,
        "findings": []
      }
    },
    {
      "schema": {
        "activity_templates": {},
        "connectors": {},
        "data_accesses": [
          {
            "element_id": "tage",
            "mandatory": true,
            "mode": "WRITE",
            "node_id": "act_2",
            "param_type": null
          },
          {
            "element_id": "tage",
            "mandatory": true,
            "mode": "READ",
            "node_id": "act_3",
            "param_type": null
          }
        ],
        "data_elements": {
          "tage": {
            "data_type": "INTEGER",
            "external": null,
            "id": "tage",
            "name": "Urlaubstage",
            "select": null,
            "source": "INSTANCE",
            "write": null
          }
        },
        "deadline_seconds": null,
        "edges": [
          {
            "condition": null,
            "source": "act_2",
            "target": "act_3",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "act_3",
            "target": "end",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "start",
            "target": "act_4",
            "type": "CONTROL"
          },
          {
            "condition": null,
            "source": "act_4",
            "target": "act_2",
            "type": "CONTROL"
          }
        ],
        "follow_up_links": [],
        "forms": {},
        "id": "tour-schema",
        "is_library_subprocess": false,
        "lifecycle_state": "ENTWURF",
        "mail_bindings": {},
        "name": "Urlaubsantrag (Tutorial)",
        "node_priorities": {},
        "nodes": {
          "act_2": {
            "id": "act_2",
            "label": "Antrag erfassen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "act_3": {
            "id": "act_3",
            "label": "Antrag prüfen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "act_4": {
            "id": "act_4",
            "label": "Resturlaub prüfen",
            "type": "ACTIVITY",
            "value_class": null
          },
          "end": {
            "id": "end",
            "label": "Ende",
            "type": "END",
            "value_class": null
          },
          "start": {
            "id": "start",
            "label": "Start",
            "type": "START",
            "value_class": null
          }
        },
        "org_model": {
          "agents": {
            "tour-a-erika": {
              "deputy_id": null,
              "email": null,
              "id": "tour-a-erika",
              "name": "Erika Sander",
              "org_unit_id": "personal",
              "role_ids": [
                "sachbearbeiter"
              ]
            },
            "tour-a-tom": {
              "deputy_id": null,
              "email": null,
              "id": "tour-a-tom",
              "name": "Tom Berger",
              "org_unit_id": "personal",
              "role_ids": [
                "teamleitung"
              ]
            }
          },
          "id": null,
          "name": "",
          "org_units": {
            "personal": {
              "id": "personal",
              "mailbox": null,
              "manager_id": "tour-a-tom",
              "name": "Personal",
              "parent_id": null
            }
          },
          "roles": {
            "sachbearbeiter": {
              "id": "sachbearbeiter",
              "mailbox": null,
              "name": "Sachbearbeiter"
            },
            "teamleitung": {
              "id": "teamleitung",
              "mailbox": null,
              "name": "Teamleitung"
            }
          }
        },
        "org_model_id": null,
        "service_bindings": {},
        "staff_rules": {
          "act_2": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "sachbearbeiter"
          },
          "act_3": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "teamleitung"
          },
          "act_4": {
            "kind": "ROLE",
            "operands": [],
            "recursive": false,
            "ref": "sachbearbeiter"
          }
        },
        "sub_process_bindings": {},
        "time_constraints": {},
        "version": 1,
        "xor_decisions": {}
      },
      "validation": {
        "correct": true,
        "findings": []
      }
    }
  ]
};
