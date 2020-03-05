import os, datetime, csv, io, math, json, uuid, locale, re, hmac, urllib.parse
from enum import Enum, auto

from flask import Flask, jsonify, request, Response, redirect, session
from flask_httpauth import HTTPBasicAuth

from sampler import Sampler
from werkzeug.exceptions import InternalServerError
from xkcdpass import xkcd_password as xp

from sqlalchemy import event, func
from sqlalchemy.dialects.postgresql import aggregate_order_by

from authlib.flask.client import OAuth

from util.binpacking import Bucket, BalancedBucketList

from arlo_server.base import app
from arlo_server.db import db
from models import *

from config import HTTP_ORIGIN
from config import AUDITADMIN_AUTH0_BASE_URL, AUDITADMIN_AUTH0_CLIENT_ID, AUDITADMIN_AUTH0_CLIENT_SECRET
from config import JURISDICTIONADMIN_AUTH0_BASE_URL, JURISDICTIONADMIN_AUTH0_CLIENT_ID, JURISDICTIONADMIN_AUTH0_CLIENT_SECRET

AUDIT_BOARD_MEMBER_COUNT = 2
WORDS = xp.generate_wordlist(wordfile=xp.locate_wordfile())


class UserType(str, Enum):
    AUDIT_ADMIN = 'audit_admin'
    JURISDICTION_ADMIN = 'jurisdiction_admin'


def create_election(election_id=None, organization_id=None):
    if not election_id:
        election_id = str(uuid.uuid4())
    if not organization_id:
        organization_id = create_organization().id
    e = Election(id=election_id, organization_id=organization_id, name="")
    db.session.add(e)
    db.session.commit()
    return election_id


def create_organization(name=""):
    org = Organization(id=str(uuid.uuid4()), name=name)
    db.session.add(org)
    db.session.commit()
    return org


def init_db():
    db.create_all()


def get_election(election_id):
    return Election.query.filter_by(id=election_id).one()


def contest_status(election):
    contests = {}

    for contest in election.contests:
        contests[contest.id] = dict([[choice.id, choice.num_votes] for choice in contest.choices])
        contests[contest.id]['ballots'] = contest.total_ballots_cast
        contests[contest.id]['numWinners'] = contest.num_winners
        contests[contest.id]['votesAllowed'] = contest.votes_allowed

    return contests


def sample_results(election):
    contests = {}

    for contest in election.contests:
        contests[contest.id] = dict([[choice.id, 0] for choice in contest.choices])

        round_contests = RoundContest.query.filter_by(
            contest_id=contest.id).order_by('round_id').all()
        for round_contest in round_contests:
            for result in round_contest.results:
                contests[contest.id][result.targeted_contest_choice_id] += result.result

    return contests


def get_sampler(election):
    # TODO Change this to audit_type
    return Sampler('BRAVO', election.random_seed, election.risk_limit / 100,
                   contest_status(election))


def compute_sample_sizes(round_contest):
    the_round = round_contest.round
    election = the_round.election
    sampler = get_sampler(election)

    # format the options properly
    raw_sample_size_options = sampler.get_sample_sizes(
        sample_results(election))[election.contests[0].id]
    sample_size_options = []
    sample_size_90 = None
    sample_size_backup = None
    for (prob_or_asn, size) in raw_sample_size_options.items():
        prob = None
        type = None

        if prob_or_asn == "asn":
            if size["prob"]:
                prob = round(size["prob"], 2),  # round to the nearest hundreth
            sample_size_options.append({
                "type": "ASN",
                "prob": prob,
                "size": int(math.ceil(size["size"]))
            })
            sample_size_backup = int(math.ceil(size["size"]))

        else:
            prob = prob_or_asn
            sample_size_options.append({"type": None, "prob": prob, "size": int(math.ceil(size))})

            # stash this one away for later
            if prob == 0.9:
                sample_size_90 = size

    round_contest.sample_size_options = json.dumps(sample_size_options)

    # if we are in multi-winner, there is no sample_size_90 so fix it
    if not sample_size_90:
        sample_size_90 = sample_size_backup

    # for later rounds, we always pick 90%
    if round_contest.round.round_num > 1:
        round_contest.sample_size = sample_size_90
        sample_ballots(election, the_round)

    db.session.commit()


def setup_next_round(election):
    if len(election.contests) > 1:
        raise Exception("only supports one contest for now")

    rounds = Round.query.filter_by(election_id=election.id).order_by('round_num').all()

    print("adding round {:d} for election {:s}".format(len(rounds) + 1, election.id))
    round = Round(id=str(uuid.uuid4()),
                  election_id=election.id,
                  round_num=len(rounds) + 1,
                  started_at=datetime.datetime.utcnow())

    db.session.add(round)

    # assume just one contest for now
    contest = election.contests[0]
    round_contest = RoundContest(round_id=round.id, contest_id=contest.id)

    db.session.add(round_contest)


def sample_ballots(election, round):
    # assume only one contest
    round_contest = round.round_contests[0]
    jurisdiction = election.jurisdictions[0]

    num_sampled = db.session.query(SampledBallotDraw).join(
        SampledBallotDraw.batch).filter_by(jurisdiction_id=jurisdiction.id).count()
    if not num_sampled:
        num_sampled = 0

    chosen_sample_size = round_contest.sample_size
    sampler = get_sampler(election)

    # the sampler needs to have the same inputs given the same manifest
    # so we use the batch name, rather than the batch id
    # (because the batch ID is an internally generated uuid
    #  that changes from one run to the next.)
    manifest = {}
    batch_id_from_name = {}
    for batch in jurisdiction.batches:
        manifest[batch.name] = batch.num_ballots
        batch_id_from_name[batch.name] = batch.id

    sample = sampler.draw_sample(manifest, chosen_sample_size, num_sampled=num_sampled)

    audit_boards = jurisdiction.audit_boards

    last_sample = None
    last_sampled_ballot = None

    batch_sizes = {}
    batches_to_ballots = {}
    # Build batch - batch_size map
    for item in sample:
        batch_name, ballot_position = item[1]
        sample_number = item[2]
        ticket_number = item[0]

        if batch_name in batch_sizes:
            if sample_number == 1:  # if we've already seen it, it doesn't affect batch size
                batch_sizes[batch_name] += 1
            batches_to_ballots[batch_name].append((ballot_position, ticket_number, sample_number))
        else:
            batch_sizes[batch_name] = 1
            batches_to_ballots[batch_name] = [(ballot_position, ticket_number, sample_number)]

    # Create the buckets and initially assign batches
    buckets = [Bucket(audit_board.name) for audit_board in audit_boards]
    for i, batch in enumerate(batch_sizes):
        buckets[i % len(audit_boards)].add_batch(batch, batch_sizes[batch])

    # Now assign batchest fairly
    bl = BalancedBucketList(buckets)

    # read audit board and batch info out
    for audit_board_num, bucket in enumerate(bl.buckets):
        audit_board = audit_boards[audit_board_num]
        for batch_name in bucket.batches:

            for item in batches_to_ballots[batch_name]:
                ballot_position, ticket_number, sample_number = item

                # sampler is 0-indexed, we're 1-indexing here
                ballot_position += 1

                batch_id = batch_id_from_name[batch_name]

                if sample_number == 1:
                    sampled_ballot = SampledBallot(batch_id=batch_id,
                                                   ballot_position=ballot_position,
                                                   audit_board_id=audit_board.id)
                    db.session.add(sampled_ballot)

                sampled_ballot_draw = SampledBallotDraw(batch_id=batch_id,
                                                        ballot_position=ballot_position,
                                                        round_id=round.id,
                                                        ticket_number=ticket_number)

                db.session.add(sampled_ballot_draw)

    db.session.commit()


def check_round(election, jurisdiction_id, round_id):
    jurisdiction = Jurisdiction.query.get(jurisdiction_id)
    round = Round.query.get(round_id)

    # assume one contest
    round_contest = round.round_contests[0]

    sampler = get_sampler(election)
    current_sample_results = sample_results(election)

    risk, is_complete = sampler.compute_risk(round_contest.contest_id,
                                             current_sample_results[round_contest.contest_id])

    round.ended_at = datetime.datetime.utcnow()
    # TODO this is a hack, should we report pairwise p-values?
    round_contest.end_p_value = max(risk.values())
    round_contest.is_complete = is_complete

    db.session.commit()

    return is_complete


def election_timestamp_name(election) -> str:
    clean_election_name = re.sub(r'[^a-zA-Z0-9]+', r'-', election.name)
    now = datetime.datetime.utcnow().isoformat(timespec='minutes')
    return f'{clean_election_name}-{now}'


def serialize_members(audit_board):
    members = []

    for i in range(0, AUDIT_BOARD_MEMBER_COUNT):
        name = getattr(audit_board, f"member_{i + 1}")
        affiliation = getattr(audit_board, f"member_{i + 1}_affiliation")

        if not name:
            break

        members.append({"name": name, "affiliation": affiliation})

    return members


ADMIN_PASSWORD = os.environ.get('ARLO_ADMIN_PASSWORD', None)

# this is a temporary approach to getting all running audits
# before we actually tie audits to a single user / login.
#
# only allow this URL if an admin password has been set.
if ADMIN_PASSWORD:
    auth = HTTPBasicAuth()

    @auth.verify_password
    def verify_password(username, password):
        # use a comparison method that prevents timing attacks:
        # https://securitypitfalls.wordpress.com/2018/08/03/constant-time-compare-in-python/
        return password is not None and hmac.compare_digest(password, ADMIN_PASSWORD)

    @app.route('/admin', methods=["GET"])
    @auth.login_required
    def admin():
        elections = Election.query.all()
        result = "\n".join(["%s - %s" % (e.id, e.name) for e in elections])
        return Response(result, content_type='text/plain')


@app.route('/election/new', methods=["POST"])
def election_new():
    election_id = create_election()
    return jsonify(electionId=election_id)


@app.route('/election/<election_id>/audit/status', methods=["GET"])
def audit_status(election_id=None):
    election = get_election(election_id)

    return jsonify(name=election.name,
                   online=election.online,
                   riskLimit=election.risk_limit,
                   randomSeed=election.random_seed,
                   contests=[{
                       "id":
                       contest.id,
                       "name":
                       contest.name,
                       "choices": [{
                           "id": choice.id,
                           "name": choice.name,
                           "numVotes": choice.num_votes
                       } for choice in contest.choices],
                       "totalBallotsCast":
                       contest.total_ballots_cast,
                       "numWinners":
                       contest.num_winners,
                       "votesAllowed":
                       contest.votes_allowed
                   } for contest in election.contests],
                   jurisdictions=[{
                       "id":
                       j.id,
                       "name":
                       j.name,
                       "contests": [c.contest_id for c in j.contests],
                       "auditBoards": [{
                           "id": audit_board.id,
                           "name": audit_board.name,
                           "members": serialize_members(audit_board),
                           "passphrase": audit_board.passphrase
                       } for audit_board in j.audit_boards],
                       "ballotManifest": {
                           "filename": j.manifest_filename,
                           "numBallots": j.manifest_num_ballots,
                           "numBatches": j.manifest_num_batches,
                           "uploadedAt": j.manifest_uploaded_at
                       },
                       "batches": [{
                           "id": batch.id,
                           "name": batch.name,
                           "numBallots": batch.num_ballots,
                           "storageLocation": batch.storage_location,
                           "tabulator": batch.tabulator
                       } for batch in j.batches]
                   } for j in election.jurisdictions],
                   rounds=[{
                       "id":
                       round.id,
                       "startedAt":
                       round.started_at,
                       "endedAt":
                       round.ended_at,
                       "contests": [{
                           "id":
                           round_contest.contest_id,
                           "endMeasurements": {
                               "pvalue": round_contest.end_p_value,
                               "isComplete": round_contest.is_complete
                           },
                           "results":
                           dict([[result.targeted_contest_choice_id, result.result]
                                 for result in round_contest.results]),
                           "sampleSizeOptions":
                           json.loads(round_contest.sample_size_options or 'null'),
                           "sampleSize":
                           round_contest.sample_size
                       } for round_contest in round.round_contests]
                   } for round in election.rounds])


@app.route('/election/<election_id>/audit/basic', methods=["POST"])
def audit_basic_update(election_id):
    election = get_election(election_id)
    info = request.get_json()
    election.name = info['name']
    election.risk_limit = info['riskLimit']
    election.random_seed = info['randomSeed']
    election.online = info['online']

    errors = []
    db.session.query(TargetedContest).filter_by(election_id=election.id).delete()

    for contest in info['contests']:
        total_allowed_votes_in_contest = contest['totalBallotsCast'] * contest['votesAllowed']

        contest_obj = TargetedContest(election_id=election.id,
                                      id=contest['id'],
                                      name=contest['name'],
                                      total_ballots_cast=contest['totalBallotsCast'],
                                      num_winners=contest['numWinners'],
                                      votes_allowed=contest['votesAllowed'])
        db.session.add(contest_obj)

        total_votes_in_all_choices = 0

        for choice in contest['choices']:
            total_votes_in_all_choices += choice['numVotes']

            choice_obj = TargetedContestChoice(id=choice['id'],
                                               contest_id=contest_obj.id,
                                               name=choice['name'],
                                               num_votes=choice['numVotes'])
            db.session.add(choice_obj)

        if total_votes_in_all_choices > total_allowed_votes_in_contest:
            errors.append({
                'message':
                f'Too many votes cast in contest: {contest["name"]} ({total_votes_in_all_choices} votes, {total_allowed_votes_in_contest} allowed)',
                'errorType': 'TooManyVotes'
            })

    if errors:
        db.session.rollback()
        return jsonify(errors=errors), 400

    # prepare the round, including sample sizes
    setup_next_round(election)

    db.session.commit()

    return jsonify(status="ok")


@app.route('/election/<election_id>/audit/sample-size', methods=["POST"])
def samplesize_set(election_id):
    election = get_election(election_id)

    # only works if there's only one round
    rounds = election.rounds
    if len(rounds) > 1:
        return jsonify(status="bad")

    rounds[0].round_contests[0].sample_size = int(request.get_json()['size'])
    db.session.commit()

    return jsonify(status="ok")


@app.route('/election/<election_id>/audit/jurisdictions', methods=["POST"])
def jurisdictions_set(election_id):
    election = get_election(election_id)
    jurisdictions = request.get_json()['jurisdictions']

    db.session.query(Jurisdiction).filter_by(election_id=election.id).delete()

    for jurisdiction in jurisdictions:
        jurisdiction_obj = Jurisdiction(election_id=election.id,
                                        id=jurisdiction['id'],
                                        name=jurisdiction['name'])
        db.session.add(jurisdiction_obj)

        for contest_id in jurisdiction["contests"]:
            jurisdiction_contest = TargetedContestJurisdiction(contest_id=contest_id,
                                                               jurisdiction_id=jurisdiction_obj.id)
            db.session.add(jurisdiction_contest)

        for audit_board in jurisdiction["auditBoards"]:
            audit_board_obj = AuditBoard(id=audit_board["id"],
                                         name=audit_board["name"],
                                         jurisdiction_id=jurisdiction_obj.id,
                                         passphrase=xp.generate_xkcdpassword(WORDS,
                                                                             numwords=4,
                                                                             delimiter="-"))
            db.session.add(audit_board_obj)

    db.session.commit()

    return jsonify(status="ok")


@app.route('/election/<election_id>/jurisdiction/<jurisdiction_id>/manifest',
           methods=["DELETE", "POST"])
def jurisdiction_manifest(jurisdiction_id, election_id):
    BATCH_NAME = 'Batch Name'
    NUMBER_OF_BALLOTS = 'Number of Ballots'
    STORAGE_LOCATION = 'Storage Location'
    TABULATOR = 'Tabulator'

    election = get_election(election_id)
    jurisdiction = Jurisdiction.query.filter_by(election_id=election.id, id=jurisdiction_id).one()

    if not jurisdiction:
        return jsonify(errors=[{
            'message': f'No jurisdiction found with id: {jurisdiction_id}',
            'errorType': 'NotFoundError'
        }]), 404

    if request.method == "DELETE":
        jurisdiction.manifest = None
        jurisdiction.manifest_filename = None
        jurisdiction.manifest_uploaded_at = None
        jurisdiction.manifest_num_ballots = None
        jurisdiction.manifest_num_batches = None

        Batch.query.filter_by(jurisdiction=jurisdiction).delete()

        db.session.commit()

        return jsonify(status="ok")

    manifest = request.files['manifest']
    manifest_string = manifest.read().decode('utf-8-sig')
    jurisdiction.manifest = manifest_string

    jurisdiction.manifest_filename = manifest.filename
    jurisdiction.manifest_uploaded_at = datetime.datetime.utcnow()

    manifest_csv = csv.DictReader(io.StringIO(manifest_string))

    missing_fields = [
        field for field in [BATCH_NAME, NUMBER_OF_BALLOTS] if field not in manifest_csv.fieldnames
    ]

    if missing_fields:
        return jsonify(errors=[{
            'message': f'Missing required CSV field "{field}"',
            'errorType': 'MissingRequiredCsvField',
            'fieldName': field
        } for field in missing_fields]), 400

    num_batches = 0
    num_ballots = 0
    for row in manifest_csv:
        num_ballots_in_batch_csv = row[NUMBER_OF_BALLOTS]

        try:
            num_ballots_in_batch = locale.atoi(num_ballots_in_batch_csv)
        except ValueError:
            return jsonify(errors=[{
                'message':
                f'Invalid value for "{NUMBER_OF_BALLOTS}" on line {manifest_csv.line_num}: {num_ballots_in_batch_csv}',
                'errorType': 'InvalidCsvIntegerField'
            }]), 400

        batch = Batch(id=str(uuid.uuid4()),
                      name=row[BATCH_NAME],
                      jurisdiction_id=jurisdiction.id,
                      num_ballots=num_ballots_in_batch,
                      storage_location=row.get(STORAGE_LOCATION, None),
                      tabulator=row.get(TABULATOR, None))
        db.session.add(batch)
        num_batches += 1
        num_ballots += batch.num_ballots

    jurisdiction.manifest_num_ballots = num_ballots
    jurisdiction.manifest_num_batches = num_batches
    db.session.commit()

    # draw the sample
    sample_ballots(election, election.rounds[0])

    return jsonify(status="ok")


@app.route('/election/<election_id>/jurisdiction/<jurisdiction_id>/audit-board/<audit_board_id>',
           methods=["GET"])
def audit_board(election_id, jurisdiction_id, audit_board_id):
    audit_boards = AuditBoard.query.filter_by(id=audit_board_id) \
        .join(AuditBoard.jurisdiction).filter_by(id=jurisdiction_id, election_id=election_id) \
        .all()

    if not audit_boards:
        return f"no audit board found with id={audit_board_id}", 404

    if len(audit_boards) > 1:
        return f"found too many audit boards with id={audit_board_id}", 400

    audit_board = audit_boards[0]

    return jsonify(id=audit_board.id, name=audit_board.name, members=serialize_members(audit_board))


@app.route('/election/<election_id>/jurisdiction/<jurisdiction_id>/audit-board/<audit_board_id>',
           methods=["POST"])
def set_audit_board(election_id, jurisdiction_id, audit_board_id):
    attributes = request.get_json()
    audit_boards = AuditBoard.query.filter_by(id=audit_board_id) \
        .join(Jurisdiction).filter_by(id=jurisdiction_id, election_id=election_id) \
        .all()

    if not audit_boards:
        return jsonify(errors=[{
            'message': f'No audit board found with id={audit_board_id}',
            'errorType': 'NotFoundError'
        }]), 404

    if len(audit_boards) > 1:
        return jsonify(errors=[{
            'message': f'Found too many audit boards with id={audit_board_id}',
            'errorType': 'BadRequest'
        }]), 400

    audit_board = audit_boards[0]
    members = attributes.get('members', None)

    if members is not None:
        if len(members) != AUDIT_BOARD_MEMBER_COUNT:
            return jsonify(errors=[{
                'message':
                f'Members must contain exactly {AUDIT_BOARD_MEMBER_COUNT} entries, got {len(members)}',
                'errorType': 'BadRequest'
            }]), 400

        for i in range(0, AUDIT_BOARD_MEMBER_COUNT):
            setattr(audit_board, f"member_{i + 1}", members[i]['name'])
            setattr(audit_board, f"member_{i + 1}_affiliation", members[i]['affiliation'])

    name = attributes.get('name', None)

    if name is not None:
        audit_board.name = name

    db.session.commit()

    return jsonify(status="ok")


@app.route('/election/<election_id>/jurisdiction/<jurisdiction_id>/round/<round_id>/ballot-list')
def ballot_list(election_id, jurisdiction_id, round_id):
    query = SampledBallotDraw.query \
                .join(SampledBallot).join(SampledBallotDraw.batch).join(AuditBoard).join(Round) \
                .add_entity(SampledBallot).add_entity(Batch).add_entity(AuditBoard) \
                .filter(Batch.jurisdiction_id == jurisdiction_id) \
                .order_by(AuditBoard.name, Batch.name, SampledBallot.ballot_position, SampledBallotDraw.ticket_number) \
                .all()

    return jsonify(ballots=[{
        "ticketNumber": ballot_draw.ticket_number,
        "status": 'AUDITED' if ballot.vote is not None else None,
        "vote": ballot.vote,
        "comment": ballot.comment,
        "position": ballot.ballot_position,
        "batch": {
            "id": batch.id,
            "name": batch.name,
            "tabulator": batch.tabulator
        },
        "auditBoard": {
            "id": audit_board.id,
            "name": audit_board.name
        }
    } for (ballot_draw, ballot, batch, audit_board) in query])


@app.route(
    '/election/<election_id>/jurisdiction/<jurisdiction_id>/audit-board/<audit_board_id>/round/<round_id>/ballot-list'
)
def ballot_list_by_audit_board(election_id, jurisdiction_id, audit_board_id, round_id):
    query = SampledBallotDraw.query \
                .join(Round).join(SampledBallot).join(Batch) \
                .add_entity(SampledBallot).add_entity(Batch) \
                .filter(Batch.jurisdiction_id == jurisdiction_id) \
                .filter(SampledBallot.audit_board_id == audit_board_id) \
                .order_by(Batch.name, SampledBallot.ballot_position, SampledBallotDraw.ticket_number)

    return jsonify(ballots=[{
        "ticketNumber": ballot_draw.ticket_number,
        "status": 'AUDITED' if ballot.vote is not None else None,
        "vote": ballot.vote,
        "comment": ballot.comment,
        "position": ballot.ballot_position,
        "batch": {
            "id": batch.id,
            "name": batch.name,
            "tabulator": batch.tabulator
        }
    } for (ballot_draw, ballot, batch) in query])


@app.route(
    '/election/<election_id>/jurisdiction/<jurisdiction_id>/batch/<batch_id>/ballot/<ballot_position>',
    methods=["POST"])
def ballot_set(election_id, jurisdiction_id, batch_id, ballot_position):
    attributes = request.get_json()
    ballots = SampledBallot.query \
        .filter_by(batch_id=batch_id, ballot_position=ballot_position) \
        .join(SampledBallot.batch) \
        .filter_by(jurisdiction_id=jurisdiction_id) \
        .all()

    if not ballots:
        return jsonify(errors=[{
            'message':
            f'No ballot found with election_id={election_id}, jurisdiction_id={jurisdiction_id}, batch_id={batch_id}, ballot_position={ballot_position}, round={round_id}',
            'errorType': 'NotFoundError'
        }]), 404
    elif len(ballots) > 1:
        return jsonify(errors=[{
            'message':
            f'Multiple ballots found with election_id={election_id}, jurisdiction_id={jurisdiction_id}, batch_id={batch_id}, ballot_position={ballot_position}, round={round_id}',
            'errorType': 'BadRequest'
        }]), 400

    ballot = ballots[0]

    if 'vote' in attributes:
        ballot.vote = attributes['vote']

    if 'comment' in attributes:
        ballot.comment = attributes['comment']

    db.session.commit()

    return jsonify(status="ok")


@app.route('/election/<election_id>/jurisdiction/<jurisdiction_id>/<round_num>/retrieval-list',
           methods=["GET"])
def jurisdiction_retrieval_list(election_id, jurisdiction_id, round_num):
    election = get_election(election_id)

    # check the jurisdiction and round
    jurisdiction = Jurisdiction.query.filter_by(election_id=election.id, id=jurisdiction_id).one()
    round = Round.query.filter_by(election_id=election.id, round_num=round_num).one()

    csv_io = io.StringIO()
    retrieval_list_writer = csv.writer(csv_io)
    retrieval_list_writer.writerow([
        "Batch Name", "Ballot Number", "Storage Location", "Tabulator", "Ticket Numbers",
        "Already Audited", "Audit Board"
    ])

    # Get previously sampled ballots as a separate query for clarity
    # (self joins are cool but they're not super clear)
    previous_ballots_query = SampledBallotDraw.query \
                        .join(SampledBallotDraw.round).filter(Round.round_num < round_num) \
                        .join(SampledBallotDraw.batch).filter_by(jurisdiction_id = jurisdiction_id)  \
                        .values(Batch.name, SampledBallotDraw.ballot_position)
    previous_ballots = {(batch_name, ballot_position)
                        for batch_name, ballot_position in previous_ballots_query}

    # Get deduped sampled ballots
    ballots = SampledBallotDraw.query.filter_by(round_id = round.id) \
                    .join(SampledBallotDraw.batch).filter_by(jurisdiction_id = jurisdiction_id)  \
                    .join(SampledBallotDraw.sampled_ballot).join(SampledBallot.audit_board) \
                    .add_entity(Batch).add_entity(AuditBoard) \
                    .group_by(Batch.name, Batch.id, Batch.storage_location, Batch.tabulator, AuditBoard.name)\
                    .group_by(SampledBallotDraw.ballot_position) \
                    .order_by(AuditBoard.name, Batch.name, SampledBallotDraw.ballot_position) \
                    .values(Batch.id, SampledBallotDraw.ballot_position, Batch.name,
                            Batch.storage_location, Batch.tabulator, AuditBoard.name,
                            func.string_agg(SampledBallotDraw.ticket_number,
                                            aggregate_order_by(",", SampledBallotDraw.ticket_number)))

    for batch_id, position, batch_name, storage_location, tabulator, audit_board, ticket_numbers in ballots:
        previously_audited = "Y" if (batch_name, position) in previous_ballots else "N"
        retrieval_list_writer.writerow([
            batch_name, position, storage_location, tabulator, ticket_numbers, previously_audited,
            audit_board
        ])

    response = Response(csv_io.getvalue())
    response.headers[
        'Content-Disposition'] = f'attachment; filename="ballot-retrieval-{election_timestamp_name(election)}.csv"'
    return response


@app.route('/election/<election_id>/jurisdiction/<jurisdiction_id>/<round_num>/results',
           methods=["POST"])
def jurisdiction_results(election_id, jurisdiction_id, round_num):
    election = get_election(election_id)
    results = request.get_json()

    # check the round ownership
    round = Round.query.filter_by(election_id=election.id, round_num=round_num).one()

    for contest in results["contests"]:
        round_contest = RoundContest.query.filter_by(contest_id=contest["id"],
                                                     round_id=round.id).one()
        RoundContestResult.query.filter_by(contest_id=contest["id"], round_id=round.id).delete()

        for choice_id, result in contest["results"].items():
            contest_result = RoundContestResult(round_id=round.id,
                                                contest_id=contest["id"],
                                                targeted_contest_choice_id=choice_id,
                                                result=result)
            db.session.add(contest_result)

    if not check_round(election, jurisdiction_id, round.id):
        setup_next_round(election)

    db.session.commit()

    return jsonify(status="ok")


@app.route('/election/<election_id>/audit/report', methods=["GET"])
def audit_report(election_id):
    election = get_election(election_id)
    jurisdiction = election.jurisdictions[0]

    csv_io = io.StringIO()
    report_writer = csv.writer(csv_io)

    contest = election.contests[0]
    choices = contest.choices

    report_writer.writerow(["Contest Name", contest.name])
    report_writer.writerow(["Number of Winners", contest.num_winners])
    report_writer.writerow(["Votes Allowed", contest.votes_allowed])
    report_writer.writerow(["Total Ballots Cast", contest.total_ballots_cast])

    for choice in choices:
        report_writer.writerow(["{:s} Votes".format(choice.name), choice.num_votes])

    report_writer.writerow(["Risk Limit", "{:d}%".format(election.risk_limit)])
    report_writer.writerow(["Random Seed", election.random_seed])

    for round in election.rounds:
        round_contest = round.round_contests[0]
        round_contest_results = round_contest.results

        report_writer.writerow(
            ["Round {:d} Sample Size".format(round.round_num), round_contest.sample_size])

        for result in round_contest.results:
            report_writer.writerow([
                "Round {:d} Audited Votes for {:s}".format(round.round_num,
                                                           result.targeted_contest_choice.name),
                result.result
            ])

        report_writer.writerow(
            ["Round {:d} P-Value".format(round.round_num), round_contest.end_p_value])
        report_writer.writerow([
            "Round {:d} Risk Limit Met?".format(round.round_num),
            'Yes' if round_contest.is_complete else 'No'
        ])

        report_writer.writerow(["Round {:d} Start".format(round.round_num), round.started_at])
        report_writer.writerow(["Round {:d} End".format(round.round_num), round.ended_at])

        ballots = SampledBallotDraw.query \
                    .filter_by(round_id = round.id) \
                    .join(SampledBallotDraw.batch).add_entity(Batch) \
                    .filter_by(jurisdiction_id = jurisdiction.id) \
                    .order_by('batch_id', 'ballot_position').all()

        report_writer.writerow([
            "Round {:d} Samples".format(round.round_num), " ".join([
                "(Batch {:s}, #{:d}, Ticket #{:s})".format(batch.name, b.ballot_position,
                                                           b.ticket_number) for b, batch in ballots
            ])
        ])

    response = Response(csv_io.getvalue())
    response.headers[
        'Content-Disposition'] = f'attachment; filename="audit-report-{election_timestamp_name(election)}.csv"'
    return response


@app.route('/election/<election_id>/audit/reset', methods=["POST"])
def audit_reset(election_id):
    # deleting the election cascades to all the data structures
    Election.query.filter_by(id=election_id).delete()
    db.session.commit()

    create_election(election_id)
    db.session.commit()

    return jsonify(status="ok")


@app.route('/auditboard/<passphrase>', methods=["GET"])
def auditboard_passphrase(passphrase):
    auditboard = AuditBoard.query.filter_by(passphrase=passphrase).one()
    return redirect("/election/%s/board/%s" % (auditboard.jurisdiction.election.id, auditboard.id))


# Test endpoint for the session.
@app.route('/incr')
def incr():
    if 'count' in session:
        session['count'] += 1
    else:
        session['count'] = 1

    return jsonify(count=session['count'])


##
## Authentication
##

AUDITADMIN_OAUTH_CALLBACK_URL = '/auth/auditadmin/callback'
JURISDICTIONADMIN_OAUTH_CALLBACK_URL = '/auth/jurisdictionadmin/callback'

oauth = OAuth(app)

auth0_aa = oauth.register(
    'auth0_aa',
    client_id=AUDITADMIN_AUTH0_CLIENT_ID,
    client_secret=AUDITADMIN_AUTH0_CLIENT_SECRET,
    api_base_url=AUDITADMIN_AUTH0_BASE_URL,
    access_token_url=f"{AUDITADMIN_AUTH0_BASE_URL}/oauth/token",
    authorize_url=f"{AUDITADMIN_AUTH0_BASE_URL}/authorize",
    client_kwargs={'scope': 'openid profile email'},
)

auth0_ja = oauth.register(
    'auth0_ja',
    client_id=JURISDICTIONADMIN_AUTH0_CLIENT_ID,
    client_secret=JURISDICTIONADMIN_AUTH0_CLIENT_SECRET,
    api_base_url=JURISDICTIONADMIN_AUTH0_BASE_URL,
    access_token_url=f"{JURISDICTIONADMIN_AUTH0_BASE_URL}/oauth/token",
    authorize_url=f"{JURISDICTIONADMIN_AUTH0_BASE_URL}/authorize",
    client_kwargs={'scope': 'openid profile email'},
)


def set_loggedin_user(user_type: UserType, user_email: str):
    session['_user'] = {'type': user_type, 'email': user_email}


def get_loggedin_user():
    user = session.get('_user', None)
    return (user['type'], user['email']) if user else (None, None)


def clear_loggedin_user():
    session['_user'] = None


@app.route('/auth/me')
def me():
    user_type, user_email = get_loggedin_user()
    if user_type:
        return jsonify(type=user_type, email=user_email)
    else:
        return jsonify()


@app.route('/auth/logout')
def logout():
    user_type, user_email = get_loggedin_user()
    if not user_type:
        return redirect("/")

    clear_loggedin_user()

    # request auth0 logout and come back here when that's done
    return_url = f"{HTTP_ORIGIN}/"
    params = urllib.parse.urlencode({'returnTo': return_url})

    base_url = AUDITADMIN_AUTH0_BASE_URL if user_type == UserType.AUDIT_ADMIN else JURISDICTIONADMIN_AUTH0_BASE_URL
    return redirect(f"{base_url}/v2/logout?{params}")


@app.route('/auth/auditadmin/start')
def auditadmin_login():
    return auth0_aa.authorize_redirect(redirect_uri=f"{HTTP_ORIGIN}{AUDITADMIN_OAUTH_CALLBACK_URL}")


@app.route(AUDITADMIN_OAUTH_CALLBACK_URL)
def auditadmin_login_callback():
    auth0_aa.authorize_access_token()
    resp = auth0_aa.get('userinfo')
    userinfo = resp.json()

    if userinfo and userinfo['email']:
        user = User.query.filter_by(email=userinfo['email']).first()
        if user and len(user.audit_administrations) > 0:
            set_loggedin_user(UserType.AUDIT_ADMIN, userinfo['email'])

    return redirect('/')


@app.route('/auth/jurisdictionadmin/start')
def jurisdictionadmin_login():
    return auth0_ja.authorize_redirect(
        redirect_uri=f"{HTTP_ORIGIN}{JURISDICTIONADMIN_OAUTH_CALLBACK_URL}")


@app.route(JURISDICTIONADMIN_OAUTH_CALLBACK_URL)
def jurisdictionadmin_login_callback():
    auth0_ja.authorize_access_token()
    resp = auth0_ja.get('userinfo')
    userinfo = resp.json()

    if userinfo and userinfo['email']:
        user = User.query.filter_by(email=userinfo['email']).first()
        if user and len(user.jurisdiction_administrations) > 0:
            set_loggedin_user(UserType.JURISDICTION_ADMIN, userinfo['email'])

    return redirect('/')


# React App
@app.route('/')
@app.route('/election/<election_id>')
@app.route('/election/<election_id>/board/<board_id>')
def serve(election_id=None, board_id=None):
    return app.send_static_file('index.html')


@app.errorhandler(InternalServerError)
def handle_500(e):
    original = getattr(e, "original_exception", None)

    if original is None:
        # direct 500 error, such as abort(500)
        return e

    # wrapped unhandled error
    return jsonify(errors=[{'message': str(original), 'errorType': type(original).__name__}]), 500