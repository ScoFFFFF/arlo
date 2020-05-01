import React, { useState, useEffect, useCallback, useContext } from 'react'
import {
  Redirect,
  useRouteMatch,
  useParams,
  RouteComponentProps,
} from 'react-router-dom'
import EstimateSampleSize from './EstimateSampleSize'
import SelectBallotsToAudit from './SelectBallotsToAudit'
import CalculateRiskMeasurement from './CalculateRiskMeasurement'
import { api, checkAndToast } from '../utilities'
import { IAudit, IErrorResponse, ElementType } from '../../types'
import ResetButton from './ResetButton'
import Wrapper from '../Atoms/Wrapper'
import Sidebar from '../Atoms/Sidebar'
import { AuthDataContext } from '../UserContext'
import Setup, { setupStages } from './Setup'
import Progress from './Progress'
import useSetupMenuItems from './useSetupMenuItems'
import BallotManifest from './Setup/BallotManifest'

interface IParams {
  electionId: string
  view: 'setup' | 'progress'
}

export const MultiJurisdictionAudit: React.FC = () => {
  const { meta } = useContext(AuthDataContext)
  switch (meta!.type) {
    case 'audit_admin':
      return <AuditAdminView />
    case 'jurisdiction_admin':
      return <JurisdictionAdminView />
    /* istanbul ignore next */
    default:
      return <>Error</>
  }
}

const AuditAdminView: React.FC = () => {
  const { electionId } = useParams<IParams>()
  const [stage, setStage] = useState<ElementType<typeof setupStages>>(
    'Participants'
  )
  const [menuItems, refresh] = useSetupMenuItems(stage, setStage, electionId)

  useEffect(() => {
    refresh()
  }, [refresh])

  const match: RouteComponentProps<IParams>['match'] | null = useRouteMatch(
    '/election/:electionId/:view?'
  )
  switch (match && match.params.view) {
    case 'setup':
      return (
        <Wrapper>
          <Sidebar title="Audit Setup" menuItems={menuItems} />
          <Setup stage={stage} refresh={refresh} menuItems={menuItems} />
        </Wrapper>
      )
    case 'progress':
      return (
        <Wrapper>
          <Sidebar
            title="Audit Progress"
            menuItems={[
              {
                title: 'Jurisdictions',
                active: true,
                state: 'live',
              },
            ]}
          />
          <Progress />
        </Wrapper>
      )
    default:
      return (
        <Wrapper>
          <p>Round management view</p>
        </Wrapper>
      )
  }
}

const JurisdictionAdminView: React.FC = () => {
  // TODO
  // Return different screens based on the result from the rounds endpoint
  return <BallotManifest />
}

const initialData: IAudit = {
  name: '',
  frozenAt: null,
  online: true,
  riskLimit: '',
  randomSeed: '',
  contests: [],
  jurisdictions: [],
  rounds: [],
  isMultiJurisdiction: false,
}

export const SingleJurisdictionAudit: React.FC = () => {
  const { electionId } = useParams<IParams>()
  const [isLoading, setIsLoading] = useState<boolean>(false)
  const [audit, setAudit] = useState(initialData)

  const getStatus = useCallback(async (): Promise<IAudit> => {
    const auditStatusOrError: IAudit | IErrorResponse = await api(
      `/election/${electionId}/audit/status`
    )
    if (checkAndToast(auditStatusOrError)) {
      return initialData
    }
    return auditStatusOrError
  }, [electionId])

  const updateAudit = useCallback(async () => {
    const auditStatus = await getStatus()
    setIsLoading(true)
    setAudit(auditStatus)
    setIsLoading(false)
  }, [getStatus])

  useEffect(() => {
    updateAudit()
  }, [updateAudit])

  if (audit.isMultiJurisdiction) {
    return <Redirect to="/" />
  }

  const showSelectBallotsToAudit =
    !!audit.contests.length &&
    audit.rounds[0].contests.every(c => !!c.sampleSizeOptions)
  const showCalculateRiskMeasurement =
    !!audit.rounds.length && audit.rounds[0].contests.every(c => !!c.sampleSize)

  return (
    <Wrapper className="single-page">
      <ResetButton
        electionId={electionId}
        disabled={!audit.contests.length || isLoading}
        updateAudit={updateAudit}
      />
      <EstimateSampleSize
        audit={audit}
        isLoading={isLoading && !showSelectBallotsToAudit}
        setIsLoading={setIsLoading}
        updateAudit={updateAudit}
        getStatus={getStatus}
        electionId={electionId}
      />
      {showSelectBallotsToAudit && (
        <SelectBallotsToAudit
          audit={audit}
          isLoading={isLoading && !showCalculateRiskMeasurement}
          setIsLoading={setIsLoading}
          updateAudit={updateAudit}
          getStatus={getStatus}
          electionId={electionId}
        />
      )}
      {showCalculateRiskMeasurement && (
        <CalculateRiskMeasurement
          audit={audit}
          isLoading={isLoading}
          setIsLoading={setIsLoading}
          updateAudit={updateAudit}
          getStatus={getStatus}
          electionId={electionId}
        />
      )}
    </Wrapper>
  )
}
